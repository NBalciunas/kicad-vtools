import os
import traceback

import pcbnew
import wx

from .fencing import generate_fencing, selected_copper
from .settings import load_settings, save_settings

ICON_PATH = os.path.join(os.path.dirname(__file__), "assets/icon.png")
GENERATORS = ["Via Fencing", "Via Stitching"]
LABEL_WIDTH = 180


class Vtools(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Vtools"
        self.category = "RF tools"
        self.description = "Generate via structures"
        self.show_toolbar_button = True
        self.icon_file_name = ICON_PATH

    def Run(self):
        # The window is modal. KiCad records an undo step only for the changes that occur before Run() returns.
        frame = wx.FindWindowByName("PcbFrame")
        board = pcbnew.GetBoard()
        settings = load_settings()

        ui = VtoolsUI(frame, board, settings)
        accepted = ui.ShowModal() == wx.ID_OK
        choice = ui.gen_choice.GetStringSelection()
        settings["position"] = list(ui.GetPosition())
        ui.Destroy()
        save_settings(settings)
        if not accepted:
            return

        try:
            with wx.BusyCursor():
                match choice:
                    case "Via Fencing":
                        message = self.run_fencing(board, settings)
                    case _:
                        message = "Something went wrong..."
        except Exception:
            wx.MessageBox(traceback.format_exc(), "Vtools Error", wx.OK | wx.ICON_ERROR, frame)
            return
        pcbnew.Refresh()
        wx.MessageBox(message, "Vtools", wx.OK | wx.ICON_INFORMATION, frame)

    def run_fencing(self, board, settings):
        values = settings["fencing"]
        result = generate_fencing(
            board, selected_copper(board),
            net=values["net"],
            grid=values["grid"],
            distance=values["distance"],
            min_distance=values["min_distance"] if values["use_min_distance"] else None,
            diameter=values["diameter"],
            hole_size=values["hole_size"],
            same_net=values["same_net"],
        )

        def length(mm):
            return format_length(mm) + " mm"

        lines = [
            f"Vias placed: {result.placed + result.filled}",
            f"    on the grid: {result.placed}",
            f"    added around obstacles: {result.filled}",
            f"    nearer than the Via Distance: {result.moved}",
            f"Positions skipped: {result.skipped}",
            "",
            f"Via Grid: {length(values['grid'])}",
        ]
        if result.grid > values["grid"] + 1e-6:
            lines.append(f"    The vias cannot be nearer. Vtools used a Via Grid of {length(result.grid)}.")
        lines.append(f"Via Distance: {length(values['distance'])}")
        if values["use_min_distance"]:
            lines.append(f"Via Min Distance: {length(values['min_distance'])}")
        lines += [
            f"Net: {values['net']}",
            f"Via Hole: {length(values['hole_size'])}, Via Diameter: {length(values['diameter'])}",
        ]
        lines.append(f"Same Net Overlap: {'yes' if values['same_net'] else 'no'}")
        return "\n".join(lines)


class VtoolsUI(wx.Dialog):
    def __init__(self, frame, board, settings):
        super().__init__(parent=frame, title="Vtools", style=wx.DEFAULT_DIALOG_STYLE)

        self.board = board
        self.settings = settings
        self.SetIcon(wx.Icon(ICON_PATH, wx.BITMAP_TYPE_PNG))

        fencing = settings["fencing"]
        design = board.GetDesignSettings()
        if fencing["diameter"] is None:
            fencing["diameter"] = pcbnew.ToMM(design.GetCurrentViaSize())
        if fencing["hole_size"] is None:
            fencing["hole_size"] = pcbnew.ToMM(design.GetCurrentViaDrill())

        self.panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        # Choice
        hbox = wx.BoxSizer(wx.HORIZONTAL)
        hbox.Add(wx.StaticText(self.panel, label="Generate:"), flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=10)
        self.gen_choice = wx.Choice(self.panel, choices=GENERATORS)
        self.gen_choice.SetSelection(0)
        hbox.Add(self.gen_choice, proportion=1, flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=10)
        vbox.Add(hbox, flag=wx.EXPAND)
        self.gen_choice.Bind(wx.EVT_CHOICE, self.on_choice)

        self.dynamic_box = wx.BoxSizer(wx.VERTICAL)
        vbox.Add(self.dynamic_box, flag=wx.EXPAND | wx.ALL, border=5)

        # Enter
        self.enter_btn = wx.Button(self.panel, label="Generate")
        vbox.Add(self.enter_btn, flag=wx.ALL | wx.CENTER, border=10)
        self.enter_btn.Bind(wx.EVT_BUTTON, self.on_enter)

        self.panel.SetSizer(vbox)
        self.update_fields("Via Fencing")
        self.place(settings["position"])

    def place(self, position):
        # Use the saved position only when it is on a connected display.
        if position and wx.Display.GetFromPoint(wx.Point(*position)) != wx.NOT_FOUND:
            self.SetPosition(wx.Point(*position))
        else:
            self.CentreOnParent()

    def on_choice(self, event):
        self.update_fields(self.gen_choice.GetStringSelection())

    def update_fields(self, choice):
        self.dynamic_box.Clear(True)
        self.length_inputs = {}

        match choice:
            case "Via Fencing":
                values = self.settings["fencing"]

                box, parent = add_box(self.panel, self.dynamic_box, "Fencing Parameters")
                self.length_inputs["grid"] = add_field(parent, box, "Via Grid:", format_length(values["grid"]), unit="mm")
                self.length_inputs["distance"] = add_field(parent, box, "Via Distance:", format_length(values["distance"]), unit="mm")
                self.length_inputs["min_distance"] = add_field(parent, box, "Via Min Distance:", format_length(values["min_distance"]), unit="mm")
                self.use_min_distance_input = add_checkbox(parent, box, "Allow Smaller Distance", default=values["use_min_distance"])
                self.use_min_distance_input.Bind(wx.EVT_CHECKBOX, lambda event: self.update_enabled())

                box, parent = add_box(self.panel, self.dynamic_box, "Via Properties")
                # Select the saved net. If it is not on this board, select a ground net.
                nets = net_names(self.board)
                preferred = [values["net"], "GND"] + [net for net in nets if "GND" in net.upper()]
                self.net_input = add_choice(parent, box, "Net:", nets, next((net for net in preferred if net in nets), None))
                self.length_inputs["hole_size"] = add_field(parent, box, "Via Hole:", format_length(values["hole_size"]), unit="mm")
                self.length_inputs["diameter"] = add_field(parent, box, "Via Diameter:", format_length(values["diameter"]), unit="mm")
                self.same_net_input = add_checkbox(parent, box, "Allow Same Net Overlap", default=values["same_net"])
                self.update_enabled()
            case "Via Stitching":
                self.dynamic_box.Add(wx.StaticText(self.panel, label="Via Stitching is not available yet."), flag=wx.ALL, border=10)
            case _:
                wx.MessageBox("Something went wrong...", "Error")

        self.enter_btn.Enable(choice == "Via Fencing")
        self.panel.Layout()
        self.panel.Fit()
        self.Fit()

    def update_enabled(self):
        self.length_inputs["min_distance"].Enable(self.use_min_distance_input.GetValue())

    def read_fields(self):
        # Copy the fields into the settings. Return an error message, or None if all values are correct.
        values = self.settings["fencing"]
        lengths = {}
        for key, ctrl in self.length_inputs.items():
            try:
                lengths[key] = parse_length(ctrl.GetValue())
            except ValueError:
                return f"\"{ctrl.GetValue()}\" is not a number."
        values.update(lengths)
        values.update(
            net=self.net_input.GetStringSelection(),
            use_min_distance=self.use_min_distance_input.GetValue(),
            same_net=self.same_net_input.GetValue(),
        )

        if values["diameter"] <= 0 or values["hole_size"] <= 0:
            return "The via hole and the via diameter must be more than zero."
        if min(values["grid"], values["distance"], values["min_distance"]) < 0:
            return "The Via Grid and the Via Distances must not be less than zero."
        if values["hole_size"] >= values["diameter"]:
            return "The via hole must be smaller than the via diameter."
        if values["use_min_distance"] and values["min_distance"] > values["distance"]:
            return "The Via Min Distance must not be larger than the Via Distance."
        if not values["net"]:
            return "The board has no nets. Add a net for the vias first."
        return None

    def on_enter(self, event):
        error = self.read_fields()
        if error:
            wx.MessageBox(error, "Vtools", wx.OK | wx.ICON_WARNING, self)
            return
        if not selected_copper(self.board):
            wx.MessageBox("No tracks, arcs or pads are selected.\nSelect the copper to fence, then start Vtools again. Vtools keeps your settings.", "Vtools", wx.OK | wx.ICON_INFORMATION, self)
            self.EndModal(wx.ID_CANCEL)
            return
        self.EndModal(wx.ID_OK)


def net_names(board):
    return sorted(str(name) for name in board.GetNetsByName().keys() if str(name))


def format_length(mm):
    return f"{mm:.4f}".rstrip("0").rstrip(".")


def parse_length(text):
    return float(text.replace(",", "."))


def add_box(panel, vbox, label):
    # The controls of a box must use the static box as their parent.
    box = wx.StaticBoxSizer(wx.VERTICAL, panel, label)
    vbox.Add(box, flag=wx.EXPAND | wx.ALL, border=5)
    return box, box.GetStaticBox()


def add_field(panel, vbox, label, default="", width=80, unit=None):
    hbox = wx.BoxSizer(wx.HORIZONTAL)
    hbox.Add(wx.StaticText(panel, label=label, size=(LABEL_WIDTH, -1)), flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=5)
    ctrl = wx.TextCtrl(panel, value=str(default), size=(width, -1))
    hbox.Add(ctrl, flag=wx.ALL | wx.CENTER, border=5)

    if unit:
        hbox.Add(wx.StaticText(panel, label=unit), flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=5)

    vbox.Add(hbox, flag=wx.EXPAND)
    return ctrl


def add_choice(panel, vbox, label, choices, default=None, width=160):
    hbox = wx.BoxSizer(wx.HORIZONTAL)
    hbox.Add(wx.StaticText(panel, label=label, size=(LABEL_WIDTH, -1)), flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=5)
    ctrl = wx.Choice(panel, choices=choices, size=(width, -1))
    ctrl.SetSelection(choices.index(default) if default in choices else (0 if choices else wx.NOT_FOUND))
    hbox.Add(ctrl, flag=wx.ALL | wx.CENTER, border=5)
    vbox.Add(hbox, flag=wx.EXPAND)
    return ctrl


def add_checkbox(panel, vbox, label, default=False):
    hbox = wx.BoxSizer(wx.HORIZONTAL)
    ctrl = wx.CheckBox(panel, label=label)
    ctrl.SetValue(default)
    hbox.Add(ctrl, flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=5)
    vbox.Add(hbox, flag=wx.ALL)
    return ctrl
