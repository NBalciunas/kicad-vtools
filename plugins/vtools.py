import os
import traceback

import pcbnew
import wx

from .fencing import generate_fencing, selected_copper
from .settings import load_settings, save_settings

MM_PER_MIL = 0.0254
ICON_PATH = os.path.join(os.path.dirname(__file__), "assets/icon.png")


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
            net_name=values["net_name"],
            via_spacing=values["via_spacing"],
            via_gap=values["via_gap"],
            min_gap=values["min_gap"] if values["use_min_gap"] else None,
            via_diameter=values["via_diameter"],
            via_drill=values["via_drill"],
            staggered=values["staggered"],
            same_net=values["same_net"],
            fill_gaps=values["fill_gaps"],
            group_name=values["group_name"],
        )

        def length(mm):
            return format_length(mm, settings["units"]) + " " + settings["units"]

        lines = [
            f"Vias placed: {result.placed + result.filled}",
            f"    on the regular spacing: {result.placed}",
            f"    added to fill gaps: {result.filled}",
            f"    moved nearer than the full gap: {result.moved}",
            f"Positions skipped: {result.skipped}",
            "",
            f"Net: {values['net_name']}",
            f"Via: {length(values['via_diameter'])}, drill {length(values['via_drill'])}",
            f"Spacing: {length(values['via_spacing'])}" + (", staggered" if values["staggered"] else ""),
            f"Gap: {length(values['via_gap'])}" + (f", down to {length(values['min_gap'])}" if values["use_min_gap"] else ""),
            f"Vias over same-net copper: {'yes' if values['same_net'] else 'no'}",
            f"Group: {values['group_name']}",
        ]
        return "\n".join(lines)


class VtoolsUI(wx.Dialog):
    def __init__(self, frame, board, settings):
        super().__init__(parent=frame, title="Vtools", style=wx.DEFAULT_DIALOG_STYLE)

        self.board = board
        self.settings = settings
        self.SetIcon(wx.Icon(ICON_PATH, wx.BITMAP_TYPE_PNG))

        fencing = settings["fencing"]
        design = board.GetDesignSettings()
        if fencing["via_diameter"] is None:
            fencing["via_diameter"] = pcbnew.ToMM(design.GetCurrentViaSize())
        if fencing["via_drill"] is None:
            fencing["via_drill"] = pcbnew.ToMM(design.GetCurrentViaDrill())

        self.panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        # Choice
        hbox = wx.BoxSizer(wx.HORIZONTAL)
        hbox.Add(wx.StaticText(self.panel, label="generate"), flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=10)
        self.gen_choice = wx.Choice(self.panel, choices=["Via Fencing"])
        self.gen_choice.SetSelection(0)
        hbox.Add(self.gen_choice, proportion=1, flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=10)
        vbox.Add(hbox, flag=wx.EXPAND)
        self.gen_choice.Bind(wx.EVT_CHOICE, self.on_choice)

        # Units
        hbox = wx.BoxSizer(wx.HORIZONTAL)
        hbox.Add(wx.StaticText(self.panel, label="units"), flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=10)
        self.mm_input = wx.RadioButton(self.panel, label="mm", style=wx.RB_GROUP)
        self.mils_input = wx.RadioButton(self.panel, label="mils")
        self.mils_input.SetValue(settings["units"] == "mils")
        self.mm_input.SetValue(settings["units"] == "mm")
        for ctrl in (self.mm_input, self.mils_input):
            hbox.Add(ctrl, flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=5)
            ctrl.Bind(wx.EVT_RADIOBUTTON, self.on_units)
        vbox.Add(hbox, flag=wx.EXPAND)

        self.dynamic_box = wx.BoxSizer(wx.VERTICAL)
        vbox.Add(self.dynamic_box, flag=wx.EXPAND | wx.ALL, border=5)

        # Enter
        enter_btn = wx.Button(self.panel, label="Generate")
        vbox.Add(enter_btn, flag=wx.ALL | wx.CENTER, border=10)
        enter_btn.Bind(wx.EVT_BUTTON, self.on_enter)

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

    def on_units(self, event):
        # Keep the values that the user typed. Then show them again in the new units.
        self.read_fields()
        self.settings["units"] = "mm" if self.mm_input.GetValue() else "mils"
        self.update_fields(self.gen_choice.GetStringSelection())

    def update_fields(self, choice):
        self.dynamic_box.Clear(True)
        self.length_inputs = {}
        units = self.settings["units"]

        match choice:
            case "Via Fencing":
                values = self.settings["fencing"]
                self.img = add_image(self.panel, self.dynamic_box, "assets/via-fencing.png", size=(270, 270))
                self.net_input = add_choice(self.panel, self.dynamic_box, "net", net_names(self.board), values["net_name"])
                for key, label in [
                    ("via_spacing", "Via Spacing (S)"),
                    ("via_gap", "Via Gap (G)"),
                    ("min_gap", "Min Via Gap (Gmin)"),
                    ("via_diameter", "Via Diameter (D)"),
                    ("via_drill", "Via Drill (d)"),
                ]:
                    self.length_inputs[key] = add_field(self.panel, self.dynamic_box, label, format_length(values[key], units), width=80, unit=units)
                self.group_name_input = add_field(self.panel, self.dynamic_box, "group name", values["group_name"], width=160)
                self.use_min_gap_input = add_checkbox(self.panel, self.dynamic_box, "Allow smaller gap (down to Gmin)", default=values["use_min_gap"])
                self.staggered_input = add_checkbox(self.panel, self.dynamic_box, "Staggered pattern", default=values["staggered"])
                self.same_net_input = add_checkbox(self.panel, self.dynamic_box, "Allow vias over same-net copper", default=values["same_net"])
                self.fill_gaps_input = add_checkbox(self.panel, self.dynamic_box, "Fill gaps", default=values["fill_gaps"])
                self.use_min_gap_input.Bind(wx.EVT_CHECKBOX, lambda event: self.update_enabled())
                self.update_enabled()
            case _:
                wx.MessageBox("Something went wrong...", "Error")

        self.panel.Layout()
        self.panel.Fit()
        self.Fit()

    def update_enabled(self):
        self.length_inputs["min_gap"].Enable(self.use_min_gap_input.GetValue())

    def read_fields(self):
        # Copy the fields into the settings. Return an error message, or None if all values are correct.
        values = self.settings["fencing"]
        lengths = {}
        for key, ctrl in self.length_inputs.items():
            try:
                lengths[key] = parse_length(ctrl.GetValue(), self.settings["units"])
            except ValueError:
                return f"\"{ctrl.GetValue()}\" is not a number."
        values.update(lengths)
        values.update(
            net_name=self.net_input.GetStringSelection(),
            group_name=self.group_name_input.GetValue().strip() or "Via Fencing",
            use_min_gap=self.use_min_gap_input.GetValue(),
            staggered=self.staggered_input.GetValue(),
            same_net=self.same_net_input.GetValue(),
            fill_gaps=self.fill_gaps_input.GetValue(),
        )

        if min(values["via_spacing"], values["via_diameter"], values["via_drill"]) <= 0:
            return "The via spacing, diameter and drill must be more than zero."
        if values["via_gap"] < 0 or values["min_gap"] < 0:
            return "The via gaps must not be less than zero."
        if values["via_drill"] >= values["via_diameter"]:
            return "The via drill must be smaller than the via diameter."
        if values["use_min_gap"] and values["min_gap"] > values["via_gap"]:
            return "The min via gap (Gmin) must not be larger than the via gap (G)."
        if not values["net_name"]:
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


def format_length(mm, units):
    value = mm if units == "mm" else mm / MM_PER_MIL
    return f"{value:.4f}".rstrip("0").rstrip(".")


def parse_length(text, units):
    value = float(text.replace(",", "."))
    return value if units == "mm" else value * MM_PER_MIL


def add_field(panel, vbox, label, default="", width=80, unit=None):
    hbox = wx.BoxSizer(wx.HORIZONTAL)
    hbox.Add(wx.StaticText(panel, label=label, size=(130, -1)), flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=5)
    ctrl = wx.TextCtrl(panel, value=str(default), size=(width, -1))
    hbox.Add(ctrl, flag=wx.ALL | wx.CENTER, border=5)

    if unit:
        hbox.Add(wx.StaticText(panel, label=unit), flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=5)

    vbox.Add(hbox, flag=wx.EXPAND)
    return ctrl


def add_choice(panel, vbox, label, choices, default=None, width=160):
    hbox = wx.BoxSizer(wx.HORIZONTAL)
    hbox.Add(wx.StaticText(panel, label=label, size=(130, -1)), flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=5)
    ctrl = wx.Choice(panel, choices=choices, size=(width, -1))
    # Select the saved net. If it is not on this board, select a ground net.
    preferred = [default, "GND"] + [choice for choice in choices if "GND" in choice.upper()]
    ctrl.SetSelection(next((choices.index(name) for name in preferred if name in choices), 0 if choices else wx.NOT_FOUND))
    hbox.Add(ctrl, flag=wx.ALL | wx.CENTER, border=5)
    vbox.Add(hbox, flag=wx.EXPAND)
    return ctrl


def add_image(panel, vbox, filename, size=None):
    base = os.path.dirname(__file__)
    path = os.path.join(base, filename)
    img = wx.Image(path, type=wx.BITMAP_TYPE_ANY)

    if size:
        img = img.Scale(size[0], size[1], quality=wx.IMAGE_QUALITY_HIGH)

    bmp = wx.Bitmap(img)
    ctrl = wx.StaticBitmap(panel, bitmap=bmp)
    vbox.Add(ctrl, flag=wx.ALL | wx.CENTER, border=5)
    return ctrl


def add_checkbox(panel, vbox, label, default=False):
    hbox = wx.BoxSizer(wx.HORIZONTAL)
    ctrl = wx.CheckBox(panel, label=label)
    ctrl.SetValue(default)
    hbox.Add(ctrl, flag=wx.ALL | wx.ALIGN_CENTER_VERTICAL, border=5)
    vbox.Add(hbox, flag=wx.ALL)
    return ctrl
