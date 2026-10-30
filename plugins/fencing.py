import math
from dataclasses import dataclass

import pcbnew

# The engine merges the selected copper into one polygon. It increases the polygon by
# the via gap and the via radius. Then it puts the vias on the edges of that polygon.
# All lengths in this file are in KiCad internal units (nm). Only generate_fencing() uses mm.

MAX_ERROR = pcbnew.FromMM(0.001)
GRID_CELL = pcbnew.FromMM(1)
TOLERANCE = 1000
FILL_SLACK = 0.95
GAP_STEPS = 10
SHARP_TURN = math.radians(10)


@dataclass
class FencingResult:
    placed: int = 0
    filled: int = 0
    moved: int = 0
    skipped: int = 0


def selected_copper(board):
    # When a footprint is selected, the function uses all of its pads.
    items = [track for track in board.GetTracks() if track.IsSelected()]
    for footprint in board.GetFootprints():
        footprint_selected = footprint.IsSelected()
        items.extend(pad for pad in footprint.Pads() if footprint_selected or pad.IsSelected())
    return items


def generate_fencing(board, items, net_name="GND", via_spacing=1.27, via_gap=0.5, min_gap=None, via_diameter=0.6, via_drill=0.3, staggered=False, same_net=False, fill_gaps=True, group_name="Via Fencing"):
    fencing = Fencing(board, items, net_name, pcbnew.FromMM(via_spacing), pcbnew.FromMM(via_gap), None if min_gap is None else pcbnew.FromMM(min_gap), pcbnew.FromMM(via_diameter), pcbnew.FromMM(via_drill), same_net)
    positions, result = fencing.plan(staggered, fill_gaps)

    group = pcbnew.PCB_GROUP(board)
    group.SetName(group_name)
    board.Add(group)
    for x, y in positions:
        via = pcbnew.PCB_VIA(board)
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetPosition(pcbnew.VECTOR2I(round(x), round(y)))
        via.SetWidth(pcbnew.FromMM(via_diameter))
        via.SetDrill(pcbnew.FromMM(via_drill))
        via.SetNet(fencing.net)
        # A free via keeps its net when no track connects to it.
        via.SetIsFree(True)
        board.Add(via)
        group.AddItem(via)
    return result


class Fencing:
    def __init__(self, board, items, net_name, spacing, gap, min_gap, diameter, drill, same_net):
        settings = board.GetDesignSettings()
        self.spacing = spacing
        self.gap = gap
        self.min_gap = min_gap
        self.radius = diameter // 2
        self.clearance = max(settings.m_NetSettings.GetDefaultNetclass().GetClearance(), settings.m_MinClearance)
        self.min_separation = max(diameter, drill + settings.m_HoleToHoleMin)
        self.net = board.FindNet(net_name)
        self.copper = copper_union(items)

        # The via center must stay inside this polygon.
        self.edge = pcbnew.SHAPE_POLY_SET()
        if board.GetBoardPolygonOutlines(self.edge, False) and self.edge.OutlineCount():
            self.edge.Inflate(-(self.radius + settings.m_CopperEdgeClearance), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, MAX_ERROR)
        else:
            self.edge = None

        self.keepouts = [zone.Outline() for zone in board.Zones() if zone.GetIsRuleArea() and zone.GetDoNotAllowVias()]

        selected = {item.m_Uuid.AsString() for item in items}
        self.obstacles = Grid()
        candidates = list(board.GetTracks())
        for footprint in board.GetFootprints():
            candidates.extend(footprint.Pads())
        for item in candidates:
            if item.m_Uuid.AsString() in selected:
                continue
            on_fence_net = item.GetNetCode() > 0 and item.GetNetCode() == self.net.GetNetCode()
            if same_net and on_fence_net and item.GetClass() != "PCB_VIA":
                continue
            reach = self.radius + max(self.clearance, item.GetOwnClearance(item.GetLayer()))
            box = item.GetBoundingBox()
            self.obstacles.add((item, reach), box.GetLeft() - reach, box.GetTop() - reach, box.GetRight() + reach, box.GetBottom() + reach)

        self.placed = Grid()

    def plan(self, staggered, fill_gaps):
        # The extra MAX_ERROR moves the chords of the round corners out to the full gap.
        outline = pcbnew.SHAPE_POLY_SET(self.copper)
        outline.Inflate(self.gap + self.radius + MAX_ERROR, pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, MAX_ERROR)
        walkers = [Walker(points) for points in outline_chains(outline)]

        positions = []
        result = FencingResult()
        starts = []
        for walker in walkers:
            start = None
            for index, s in enumerate(walker.positions(self.spacing)):
                if staggered and index % 2:
                    continue
                spot = self.find_spot(*walker.at(s), self.min_separation)
                if spot is None:
                    result.skipped += 1
                    continue
                positions.append(self.commit(spot, result))
                result.placed += 1
                if start is None:
                    start = s
            starts.append(start or 0.0)

        if fill_gaps:
            wanted = self.spacing * (2 if staggered else 1) * FILL_SLACK
            step = max(self.spacing / 10, pcbnew.FromMM(0.02))
            for walker, start in zip(walkers, starts):
                for index in range(int(walker.length / step)):
                    point, tangent = walker.at(start + index * step)
                    # Go to the next position when a via is too near. This makes the check faster.
                    if self.nearest_via(*point) < wanted - self.gap:
                        continue
                    spot = self.find_spot(point, tangent, wanted)
                    if spot is not None:
                        positions.append(self.commit(spot, result))
                        result.filled += 1
        return positions, result

    def commit(self, spot, result):
        x, y, moved = spot
        reach = max(self.min_separation, self.spacing * 2)
        self.placed.add((x, y), x - reach, y - reach, x + reach, y + reach)
        result.moved += moved
        return x, y

    def gaps(self):
        if self.min_gap is None or self.min_gap >= self.gap:
            return [self.gap]
        return [self.gap - (self.gap - self.min_gap) * step / GAP_STEPS for step in range(GAP_STEPS + 1)]

    def find_spot(self, point, tangent, min_distance):
        # Try the full gap first. Then move the via nearer to the copper, one gap step at a time.
        (x, y), (tx, ty) = point, tangent
        nx, ny = -ty, tx
        probe = pcbnew.VECTOR2I(int(x + nx * 10000), int(y + ny * 10000))
        if self.copper.SquaredDistance(probe) < self.copper.SquaredDistance(pcbnew.VECTOR2I(int(x), int(y))):
            nx, ny = -nx, -ny
        for gap in self.gaps():
            inset = self.gap - gap
            cx, cy = x - nx * inset, y - ny * inset
            if inset and not self.gap_ok(cx, cy, gap):
                continue
            if self.nearest_via(cx, cy) < min_distance:
                continue
            if self.free(cx, cy):
                return cx, cy, inset > 0
        return None

    def gap_ok(self, x, y, gap):
        need = gap + self.radius - TOLERANCE
        return self.copper.SquaredDistance(pcbnew.VECTOR2I(round(x), round(y))) >= need * need

    def free(self, x, y):
        point = pcbnew.VECTOR2I(round(x), round(y))
        if self.edge is not None and not self.edge.Contains(point):
            return False
        if any(keepout.Contains(point, -1, self.radius) for keepout in self.keepouts):
            return False
        return not any(item.HitTest(point, reach) for item, reach in self.obstacles.near(x, y))

    def nearest_via(self, x, y):
        return min((math.hypot(px - x, py - y) for px, py in self.placed.near(x, y)), default=math.inf)


def copper_union(items):
    poly = pcbnew.SHAPE_POLY_SET()
    for item in items:
        for layer in copper_layers(item):
            item.TransformShapeToPolygon(poly, layer, 0, MAX_ERROR, pcbnew.ERROR_INSIDE)
    poly.Simplify()
    return poly


def copper_layers(item):
    if item.GetClass() == "PAD":
        layers = [layer for layer in (pcbnew.F_Cu, pcbnew.B_Cu) if item.IsOnLayer(layer)]
        return layers or [item.GetPrincipalLayer()]
    if item.GetClass() == "PCB_VIA":
        return [pcbnew.F_Cu]
    return [item.GetLayer()]


def outline_chains(poly):
    # Each outline and each hole gives one closed chain of points.
    chains = []
    for outline in range(poly.OutlineCount()):
        lines = [poly.Outline(outline)] + [poly.Hole(outline, hole) for hole in range(poly.HoleCount(outline))]
        for line in lines:
            points = [(float(line.CPoint(i).x), float(line.CPoint(i).y)) for i in range(line.PointCount())]
            points = remove_collinear(points)
            if len(points) >= 3:
                chains.append(points)
    return chains


def remove_collinear(points):
    result = []
    count = len(points)
    for i, (x, y) in enumerate(points):
        px, py = points[i - 1]
        nx, ny = points[(i + 1) % count]
        cross = (x - px) * (ny - y) - (y - py) * (nx - x)
        dot = (x - px) * (nx - x) + (y - py) * (ny - y)
        if dot <= 0 or abs(cross) > 1e-6 * math.hypot(x - px, y - py) * math.hypot(nx - x, ny - y):
            result.append((x, y))
    return result


class Walker:
    # This class gives the point and the direction at a distance along a closed chain.

    def __init__(self, points):
        self.points = points + [points[0]]
        self.cumulative = [0.0]
        for (x0, y0), (x1, y1) in zip(self.points, self.points[1:]):
            self.cumulative.append(self.cumulative[-1] + math.hypot(x1 - x0, y1 - y0))
        self.length = self.cumulative[-1]
        self.segment = 0

    def at(self, s):
        s %= self.length
        segment = self.segment if self.cumulative[self.segment] <= s else 0
        while self.cumulative[segment + 1] < s:
            segment += 1
        self.segment = segment
        (x0, y0), (x1, y1) = self.points[segment], self.points[segment + 1]
        length = self.cumulative[segment + 1] - self.cumulative[segment]
        if length == 0:
            return (x0, y0), (1.0, 0.0)
        t = (s - self.cumulative[segment]) / length
        return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t), ((x1 - x0) / length, (y1 - y0) / length)

    def anchors(self, spacing):
        # An anchor is a sharp corner, or a point where a straight edge meets a round corner.
        # Each anchor gets a via. Because of this, parallel edges get the same via pattern.
        count = len(self.points) - 1
        lengths = [self.cumulative[i + 1] - self.cumulative[i] for i in range(count)]
        found = []
        for i in range(count):
            before, after = lengths[i - 1], lengths[i]
            if not before or not after:
                continue
            (ax, ay), (bx, by), (cx, cy) = self.points[i - 1 if i else count - 1], self.points[i], self.points[i + 1]
            turn = abs(math.atan2((bx - ax) * (cy - by) - (by - ay) * (cx - bx), (bx - ax) * (cx - bx) + (by - ay) * (cy - by)))
            longest, shortest = max(before, after), min(before, after)
            if turn > SHARP_TURN or (longest >= spacing and shortest < longest / 4):
                found.append(self.cumulative[i])
        return merge_near(found, spacing / 2, self.length)

    def positions(self, spacing):
        # Divide each section between two anchors into equal steps. No step is longer than the spacing.
        anchors = self.anchors(spacing) or [0.0]
        result = []
        for i, start in enumerate(anchors):
            end = anchors[i + 1] if i + 1 < len(anchors) else anchors[0] + self.length
            length = end - start
            steps = max(1, math.ceil(length / spacing - 1e-6))
            result.extend(start + length * k / steps for k in range(steps))
        return result


def merge_near(values, limit, period):
    # Replace each group of near values with their average. The values are on a closed loop.
    if not values:
        return []
    values = sorted(values)
    groups = [[values[0]]]
    for value in values[1:]:
        if value - groups[-1][-1] < limit:
            groups[-1].append(value)
        else:
            groups.append([value])
    if len(groups) > 1 and groups[0][0] + period - groups[-1][-1] < limit:
        groups[0] = [value - period for value in groups.pop()] + groups[0]
    return sorted((sum(group) / len(group)) % period for group in groups)


class Grid:
    # This class puts each item into the 1 mm cells that its bounding box touches.
    # A check looks at one cell only.

    def __init__(self):
        self.cells = {}

    def add(self, item, x0, y0, x1, y1):
        for cx in range(int(x0 // GRID_CELL), int(x1 // GRID_CELL) + 1):
            for cy in range(int(y0 // GRID_CELL), int(y1 // GRID_CELL) + 1):
                self.cells.setdefault((cx, cy), []).append(item)

    def near(self, x, y):
        return self.cells.get((int(x // GRID_CELL), int(y // GRID_CELL)), ())
