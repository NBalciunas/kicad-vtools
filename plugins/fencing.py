import bisect
import math
from dataclasses import dataclass

import pcbnew

# The engine merges the selected copper into one polygon. It increases the polygon by
# the distance and the via radius. Then it puts the vias on the edges of that polygon.
# The names follow the Altium "Add Shielding to Net" dialog:
#   distance: from the edge of the selected copper to the edge of a via.
#   grid: from the edge of a via to the edge of the next via.
# All lengths in this file are in KiCad internal units (nm). Only generate_fencing() uses mm.

MAX_ERROR = pcbnew.FromMM(0.001)
CELL_SIZE = pcbnew.FromMM(1)
TOLERANCE = 1000
FILL_SLACK = 0.95
DISTANCE_STEPS = 10
SHARP_TURN = math.radians(10)
SMOOTHING = 2
GROUP_NAME = "Via Fencing"


@dataclass
class FencingResult:
    placed: int = 0
    filled: int = 0
    moved: int = 0
    skipped: int = 0
    grid: float = 0.0


def selected_copper(board):
    # When a footprint is selected, the function uses all of its pads.
    items = [track for track in board.GetTracks() if track.IsSelected()]
    for footprint in board.GetFootprints():
        footprint_selected = footprint.IsSelected()
        items.extend(pad for pad in footprint.Pads() if footprint_selected or pad.IsSelected())
    return items


def generate_fencing(board, items, net="GND", grid=0.5, distance=0.5, min_distance=None, diameter=0.6, hole_size=0.3, same_net=False):
    fencing = Fencing(board, items, net, pcbnew.FromMM(grid + diameter), pcbnew.FromMM(distance), None if min_distance is None else pcbnew.FromMM(min_distance), pcbnew.FromMM(diameter), pcbnew.FromMM(hole_size), same_net)
    positions, result = fencing.plan()
    result.grid = pcbnew.ToMM(fencing.pitch) - diameter

    group = pcbnew.PCB_GROUP(board)
    group.SetName(GROUP_NAME)
    board.Add(group)
    for x, y in positions:
        via = pcbnew.PCB_VIA(board)
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetPosition(pcbnew.VECTOR2I(round(x), round(y)))
        via.SetWidth(pcbnew.FromMM(diameter))
        via.SetDrill(pcbnew.FromMM(hole_size))
        via.SetNet(fencing.net)
        # A free via keeps its net when no track connects to it.
        via.SetIsFree(True)
        board.Add(via)
        group.AddItem(via)
    return result


class Fencing:
    def __init__(self, board, items, net, pitch, distance, min_distance, diameter, hole_size, same_net):
        settings = board.GetDesignSettings()
        self.distance = distance
        self.min_distance = min_distance
        self.radius = diameter // 2
        self.clearance = max(settings.m_NetSettings.GetDefaultNetclass().GetClearance(), settings.m_MinClearance)
        # The pitch is the distance between the centers of two adjacent vias (grid + diameter).
        # Two vias cannot be nearer than the min separation. A smaller pitch changes to this value.
        self.min_separation = max(diameter, hole_size + settings.m_HoleToHoleMin) + TOLERANCE
        self.pitch = max(pitch, self.min_separation)
        self.net = board.FindNet(net)
        self.copper = copper_union(items)
        # Each component is one piece of connected copper (for example one track with its pads).
        self.components = [self.copper.UnitSet(i) for i in range(self.copper.OutlineCount())]

        # The via center must stay inside this polygon.
        self.edge = pcbnew.SHAPE_POLY_SET()
        if board.GetBoardPolygonOutlines(self.edge, False) and self.edge.OutlineCount():
            self.edge.Inflate(-(self.radius + settings.m_CopperEdgeClearance), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, MAX_ERROR)
        else:
            self.edge = None

        self.keepouts = [zone.Outline() for zone in board.Zones() if zone.GetIsRuleArea() and zone.GetDoNotAllowVias()]

        selected = {item.m_Uuid.AsString() for item in items}
        self.obstacles = CellIndex()
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

        self.placed = CellIndex()

    def fence_outline(self, component):
        # Increase the copper by the full distance. The extra MAX_ERROR moves the chords of the round corners out to the full distance.
        # Then increase and decrease by the same value. This fills the steps between a wide pad and a narrow track.
        # Each component gets its own outline. Because of this, the outlines of two near tracks do not join.
        smoothing = self.pitch * SMOOTHING
        outline = pcbnew.SHAPE_POLY_SET(component)
        outline.Inflate(self.distance + self.radius + MAX_ERROR + smoothing, pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, MAX_ERROR)
        outline.Inflate(-smoothing, pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, MAX_ERROR)
        return outline

    def plan(self):
        walkers = [Walker(points, index) for index, component in enumerate(self.components) for points in outline_chains(self.fence_outline(component))]
        result = FencingResult()
        lines = []

        for walker in walkers:
            for start, length in walker.sections(self.pitch):
                # No step is longer than the pitch, and no step is shorter than the min separation.
                steps = max(1, min(math.ceil(length / self.pitch - 1e-6), math.floor(length / self.min_separation)))
                offsets = [start + length * k / steps for k in range(steps)]
                distance, spots, own = self.best_line(walker, offsets)
                for x, y in spots:
                    self.commit(x, y)
                result.placed += len(spots)
                lines.append([walker, start, length, distance, spots, own])

        # Lay out again each section that lost vias to obstacles. The other sections do not change.
        for line in lines:
            walker, start, length, distance, spots, own = line
            if len(spots) == own:
                continue
            for x, y in spots:
                self.uncommit(x, y)
            even = self.even_out(walker, start, length, distance)
            if len(even) < len(spots):
                even = spots
            for x, y in even:
                self.commit(x, y)
            result.filled += len(even) - len(spots)
            line[4] = even

        positions = []
        for walker, start, length, distance, spots, own in lines:
            positions.extend(spots)
            result.skipped += max(0, own - len(spots))
            result.moved += len(spots) if distance < self.distance else 0
        return positions, result

    def even_out(self, walker, start, length, distance):
        # Put the most vias that fit on the section line. No step along the line is shorter than the pitch.
        # Then move the vias as near as possible to equal steps.
        # A free position has no obstacle, and no via of a different section is nearer than one pitch.
        step = max(self.pitch / 10, pcbnew.FromMM(0.02))
        count = max(2, int(length / step) + 1)
        wanted = max(self.pitch * FILL_SLACK, self.min_separation)
        free = []
        for k in range(count):
            s = start + length * k / (count - 1)
            spot = self.spot_at(walker, s, distance)
            if spot and self.nearest_via(*spot) >= wanted and self.free(*spot):
                free.append((s, spot))
        if not free:
            return []

        # Pack from the start and from the end. Via i can go anywhere between earliest[i] and latest[i].
        earliest = [free[0][0]]
        for s, _ in free:
            if s >= earliest[-1] + self.pitch:
                earliest.append(s)
        latest = [free[-1][0]]
        for s, _ in reversed(free):
            if s <= latest[-1] - self.pitch:
                latest.append(s)
        latest.reverse()
        vias = len(earliest)

        # Aim each via at an equal step. Keep it in its range, and not nearer than the pitch to the via before it.
        chosen = []
        first, last = earliest[0], latest[-1]
        for i in range(vias):
            target = first + (last - first) * i / (vias - 1) if vias > 1 else (first + last) / 2
            low = max(earliest[i], chosen[-1][0] + self.pitch if chosen else earliest[i])
            target = min(max(target, low), latest[i])
            candidates = [item for item in free if low <= item[0] <= latest[i]]
            chosen.append(min(candidates, key=lambda item: abs(item[0] - target)))

        spots = []
        for _, spot in chosen:
            if spots and math.hypot(spot[0] - spots[-1][0], spot[1] - spots[-1][1]) < self.min_separation:
                continue
            spots.append(spot)
        return spots

    def best_line(self, walker, offsets):
        # All vias of one section use the same distance. Because of this, the row stays straight.
        # The function uses the distance that gives the most vias. If two distances give the same number, it uses the larger distance.
        # The function also gives the number of positions that this line owns.
        best_distance, best_spots, own = self.distance, [], len(offsets)
        for distance in self.distances():
            spots = []
            own = 0
            for s in offsets:
                spot = self.spot_at(walker, s, distance)
                if spot is False:
                    continue
                own += 1
                if spot is None or self.nearest_via(*spot) < self.min_separation:
                    continue
                if spots and math.hypot(spot[0] - spots[-1][0], spot[1] - spots[-1][1]) < self.min_separation:
                    continue
                if self.free(*spot):
                    spots.append(spot)
            if len(spots) > len(best_spots):
                best_distance, best_spots = distance, spots
            if len(spots) == own:
                break
        return best_distance, best_spots, own

    def spot_at(self, walker, s, distance):
        # The function gives (x, y), or None if no via fits, or False if a different line owns this position.
        (x, y), (tx, ty) = walker.at(s)
        own = self.components[walker.component]
        nx, ny = -ty, tx
        probe = pcbnew.VECTOR2I(int(x + nx * 10000), int(y + ny * 10000))
        if own.SquaredDistance(probe) < own.SquaredDistance(pcbnew.VECTOR2I(int(x), int(y))):
            nx, ny = -nx, -ny

        # When the line of a different component is nearer than one pitch, the two lines share one row of vias.
        # The line of the component with the lower index puts the shared vias on the middle line.
        other, other_distance = self.nearest_other(walker.component, x, y)
        if other is not None and other_distance - (self.distance + self.radius) < self.pitch:
            shared = self.middle(x, y, nx, ny, walker.component, other, other_distance)
            if shared is not None:
                if other < walker.component:
                    return False
                mx, my, middle_distance = shared
                smallest = self.min_distance if self.min_distance is not None else self.distance
                return (mx, my) if middle_distance - self.radius >= min(smallest, self.distance) - TOLERANCE else None

        # Move the point on the outline nearer to the copper, until the distance is correct.
        inset = self.distance - distance
        if not inset:
            return x, y
        x, y = x - nx * inset, y - ny * inset
        return (x, y) if self.distance_ok(x, y, distance) else None

    def nearest_other(self, index, x, y):
        point = pcbnew.VECTOR2I(round(x), round(y))
        best, best_distance = None, math.inf
        for other, component in enumerate(self.components):
            if other != index:
                distance = math.sqrt(component.SquaredDistance(point))
                if distance < best_distance:
                    best, best_distance = other, distance
        return best, best_distance

    def middle(self, x, y, nx, ny, index, other, other_distance):
        # Find the point on the normal that has the same distance to the two components.
        # The function gives (x, y, distance to the copper centers' edge), or None if that point is too far from the two lines.
        # A far point occurs where the two lines go apart, for example at the end of a track. There, each line keeps its own vias.
        t = (other_distance - (self.distance + self.radius)) / 2
        for _ in range(4):
            point = pcbnew.VECTOR2I(round(x + nx * t), round(y + ny * t))
            own_distance = math.sqrt(self.components[index].SquaredDistance(point))
            other_distance = math.sqrt(self.components[other].SquaredDistance(point))
            t += (other_distance - own_distance) / 2
        mx, my = x + nx * t, y + ny * t
        point = pcbnew.VECTOR2I(round(mx), round(my))
        middle_distance = min(math.sqrt(self.components[index].SquaredDistance(point)), math.sqrt(self.components[other].SquaredDistance(point)))
        if middle_distance > self.distance + self.radius + self.pitch * 0.6:
            return None
        return mx, my, middle_distance

    def commit(self, x, y):
        reach = max(self.min_separation, self.pitch * 2)
        self.placed.add((x, y), x - reach, y - reach, x + reach, y + reach)
        return x, y

    def uncommit(self, x, y):
        reach = max(self.min_separation, self.pitch * 2)
        self.placed.remove((x, y), x - reach, y - reach, x + reach, y + reach)

    def distances(self):
        if self.min_distance is None or self.min_distance >= self.distance:
            return [self.distance]
        return [self.distance - (self.distance - self.min_distance) * step / DISTANCE_STEPS for step in range(DISTANCE_STEPS + 1)]

    def distance_ok(self, x, y, distance):
        need = distance + self.radius - TOLERANCE
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

    def __init__(self, points, component=0):
        self.component = component
        self.points = points + [points[0]]
        self.cumulative = [0.0]
        for (x0, y0), (x1, y1) in zip(self.points, self.points[1:]):
            self.cumulative.append(self.cumulative[-1] + math.hypot(x1 - x0, y1 - y0))
        self.length = self.cumulative[-1]

    def at(self, s):
        s %= self.length
        segment = min(bisect.bisect_right(self.cumulative, s) - 1, len(self.points) - 2)
        (x0, y0), (x1, y1) = self.points[segment], self.points[segment + 1]
        length = self.cumulative[segment + 1] - self.cumulative[segment]
        if length == 0:
            return (x0, y0), (1.0, 0.0)
        t = (s - self.cumulative[segment]) / length
        return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t), ((x1 - x0) / length, (y1 - y0) / length)

    def anchors(self, pitch):
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
            if turn > SHARP_TURN or (longest >= pitch and shortest < longest / 4):
                found.append(self.cumulative[i])
        return merge_near(found, pitch / 2, self.length)

    def sections(self, pitch):
        # Each section goes from one anchor to the next anchor. The function gives (start, length) for each section.
        anchors = self.anchors(pitch) or [0.0]
        ends = anchors[1:] + [anchors[0] + self.length]
        return [(start, end - start) for start, end in zip(anchors, ends)]


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


class CellIndex:
    # This class puts each item into the 1 mm cells that its bounding box touches.
    # A check looks at one cell only.

    def __init__(self):
        self.cells = {}

    def add(self, item, x0, y0, x1, y1):
        for cx in range(int(x0 // CELL_SIZE), int(x1 // CELL_SIZE) + 1):
            for cy in range(int(y0 // CELL_SIZE), int(y1 // CELL_SIZE) + 1):
                self.cells.setdefault((cx, cy), []).append(item)

    def remove(self, item, x0, y0, x1, y1):
        for cx in range(int(x0 // CELL_SIZE), int(x1 // CELL_SIZE) + 1):
            for cy in range(int(y0 // CELL_SIZE), int(y1 // CELL_SIZE) + 1):
                self.cells[(cx, cy)].remove(item)

    def near(self, x, y):
        return self.cells.get((int(x // CELL_SIZE), int(y // CELL_SIZE)), ())
