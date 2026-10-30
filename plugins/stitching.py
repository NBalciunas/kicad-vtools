import math
from dataclasses import dataclass

import pcbnew

from .fencing import CellIndex, FILL_SLACK, MAX_ERROR, TOLERANCE, add_vias

# The engine puts vias on a square grid in the selected zones. The grid is at the center of the zones.
# The names are the same as in fencing:
#   grid: from the edge of a via to the edge of the next via.
#   clearance: from the edge of a via to the zone edge and to copper of a different net.
# All lengths in this file are in KiCad internal units (nm). Only generate_stitching() uses mm.

GROUP_NAME = "Via Stitching"
# Shifts for a run of blocked grid points, as fractions of the pitch. The smallest shifts come first.
NUDGES = [(0, -0.25), (0, 0.25), (-0.25, 0), (0.25, 0), (0, -0.5), (0, 0.5), (-0.5, 0), (0.5, 0), (-0.5, -0.5), (0.5, -0.5), (-0.5, 0.5), (0.5, 0.5)]


@dataclass
class StitchingResult:
    placed: int = 0
    nudged: int = 0
    reduced: int = 0
    skipped: int = 0
    grid: float = 0.0


def selected_zones(board):
    return [zone for zone in board.Zones() if zone.IsSelected() and not zone.GetIsRuleArea()]


def generate_stitching(board, zones, net="GND", grid=1.0, clearance=0.5, min_clearance=None, diameter=0.4, hole_size=0.2, same_net=False):
    stitching = Stitching(board, zones, net, pcbnew.FromMM(grid + diameter), pcbnew.FromMM(clearance), None if min_clearance is None else pcbnew.FromMM(min_clearance), pcbnew.FromMM(diameter), pcbnew.FromMM(hole_size), same_net)
    positions, result = stitching.plan()
    result.grid = pcbnew.ToMM(stitching.pitch) - diameter
    add_vias(board, positions, stitching.net, diameter, hole_size, GROUP_NAME)
    # Fill the zones again. Then the copper connects to the new vias.
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    return result


class Stitching:
    def __init__(self, board, zones, net, pitch, clearance, min_clearance, diameter, hole_size, same_net):
        settings = board.GetDesignSettings()
        self.radius = diameter // 2
        self.board_clearance = max(settings.m_NetSettings.GetDefaultNetclass().GetClearance(), settings.m_MinClearance)
        # The pitch is the distance between the centers of two adjacent vias (grid + diameter).
        # Two vias cannot be nearer than the minimum separation. If the pitch is smaller, the engine uses the minimum separation.
        self.min_separation = max(diameter, hole_size + settings.m_HoleToHoleMin) + TOLERANCE
        self.pitch = max(pitch, self.min_separation)
        # The engine tries the default clearance first. If the via does not fit, it tries the minimum clearance.
        self.clearances = [clearance]
        if min_clearance is not None and min_clearance < clearance:
            self.clearances.append(min_clearance)
        self.net = board.FindNet(net)
        self.zones = zones

        # For each clearance, the via center must stay inside one of these polygons.
        self.areas = {}
        for value in self.clearances:
            polygons = []
            for zone in zones:
                area = pcbnew.SHAPE_POLY_SET(zone.Outline())
                area.Inflate(-(self.radius + value), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, MAX_ERROR)
                polygons.append(area)
            self.areas[value] = polygons

        self.edge = pcbnew.SHAPE_POLY_SET()
        if board.GetBoardPolygonOutlines(self.edge, False) and self.edge.OutlineCount():
            self.edge.Inflate(-(self.radius + settings.m_CopperEdgeClearance), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, MAX_ERROR)
        else:
            self.edge = None

        self.keepouts = [zone.Outline() for zone in board.Zones() if zone.GetIsRuleArea() and zone.GetDoNotAllowVias()]

        # Copper of a different net must keep the DRC clearance. Copper of the via net uses only the stitching clearance.
        selected = {zone.m_Uuid.AsString() for zone in zones}
        self.other_zones = []
        for zone in board.Zones():
            if zone.GetIsRuleArea() or zone.m_Uuid.AsString() in selected or zone.GetNetCode() == self.net.GetNetCode():
                continue
            if zone.IsFilled():
                self.other_zones.append((zone, list(zone.GetLayerSet().CuStack())))

        self.obstacles = CellIndex()
        self.placed = CellIndex()
        candidates = list(board.GetTracks())
        for footprint in board.GetFootprints():
            candidates.extend(footprint.Pads())
        for item in candidates:
            on_via_net = item.GetNetCode() > 0 and item.GetNetCode() == self.net.GetNetCode()
            if item.GetClass() == "PCB_VIA" and on_via_net:
                # A via of the same net only keeps the grid away from it. It does not need a clearance.
                position = item.GetPosition()
                self.commit(position.x, position.y)
                continue
            if same_net and on_via_net:
                continue
            floor = 0 if on_via_net else max(self.board_clearance, item.GetOwnClearance(item.GetLayer()))
            reach = self.radius + max(clearance, floor)
            box = item.GetBoundingBox()
            self.obstacles.add((item, floor), box.GetLeft() - reach, box.GetTop() - reach, box.GetRight() + reach, box.GetBottom() + reach)

    def plan(self):
        result = StitchingResult()
        positions = []
        blocked = {}
        for row, column, x, y in self.grid_points():
            clearance = self.check(x, y)
            if clearance is None:
                blocked.setdefault(row, []).append((column, x, y))
            else:
                positions.append(self.commit(x, y))
                result.reduced += clearance != self.clearances[0]
        result.placed = len(positions)

        # Move each run of adjacent blocked grid points in a row by the same shift.
        # Because of this, the moved vias stay in a straight row.
        # Use the shift that gives the most vias. If two shifts give the same number, use the smaller shift.
        for row in sorted(blocked):
            for run in adjacent_runs(blocked[row]):
                best = []
                for fx, fy in NUDGES:
                    spots = []
                    for x, y in run:
                        x, y = x + fx * self.pitch, y + fy * self.pitch
                        clearance = self.check(x, y)
                        if clearance is not None:
                            spots.append((x, y, clearance))
                    if len(spots) > len(best):
                        best = spots
                for x, y, clearance in best:
                    positions.append(self.commit(x, y))
                    result.reduced += clearance != self.clearances[0]
                result.nudged += len(best)
                result.skipped += len(run) - len(best)
        return positions, result

    def grid_points(self):
        # The function gives the grid points where a via can fit in a zone with the smallest clearance.
        # The points nearer to the zone edge are not part of the grid. The grid is at the center of the bounding box of all the zones.
        boxes = [zone.GetBoundingBox() for zone in self.zones]
        left, right = min(box.GetLeft() for box in boxes), max(box.GetRight() for box in boxes)
        top, bottom = min(box.GetTop() for box in boxes), max(box.GetBottom() for box in boxes)
        cx, cy = (left + right) / 2, (top + bottom) / 2
        columns = int((right - left) / 2 / self.pitch) + 1
        rows = int((bottom - top) / 2 / self.pitch) + 1
        areas = self.areas[self.clearances[-1]]
        for row in range(-rows, rows + 1):
            for column in range(-columns, columns + 1):
                x, y = cx + column * self.pitch, cy + row * self.pitch
                point = pcbnew.VECTOR2I(round(x), round(y))
                if any(area.Contains(point) for area in areas):
                    yield row, column, x, y

    def check(self, x, y):
        # Try the default clearance first. Then try the minimum clearance.
        # The function gives the clearance that fits. It gives None if the via does not fit.
        if self.nearest_via(x, y) < self.pitch * FILL_SLACK:
            return None
        for value in self.clearances:
            if self.fits(x, y, value):
                return value
        return None

    def fits(self, x, y, clearance):
        point = pcbnew.VECTOR2I(round(x), round(y))
        if not any(area.Contains(point) for area in self.areas[clearance]):
            return False
        if self.edge is not None and not self.edge.Contains(point):
            return False
        if any(keepout.Contains(point, -1, self.radius) for keepout in self.keepouts):
            return False
        if any(item.HitTest(point, self.radius + max(clearance, floor)) for item, floor in self.obstacles.near(x, y)):
            return False
        reach = self.radius + max(clearance, self.board_clearance)
        return not any(zone.HitTestFilledArea(layer, point, reach) for zone, layers in self.other_zones for layer in layers)

    def commit(self, x, y):
        reach = self.pitch
        self.placed.add((x, y), x - reach, y - reach, x + reach, y + reach)
        return x, y

    def nearest_via(self, x, y):
        return min((math.hypot(px - x, py - y) for px, py in self.placed.near(x, y)), default=math.inf)


def adjacent_runs(points):
    # Divide the (column, x, y) points of one row into runs of adjacent columns.
    # The function gives each run as a list of (x, y).
    runs = []
    for column, x, y in sorted(points):
        if runs and column == runs[-1][0] + 1:
            runs[-1][0] = column
            runs[-1][1].append((x, y))
        else:
            runs.append([column, [(x, y)]])
    return [run for _, run in runs]
