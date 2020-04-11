"""Controller for a single SSB editor (SSBScript + ExplorerScript)."""
#  Copyright 2020 Parakoopa
#
#  This file is part of SkyTemple.
#
#  SkyTemple is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SkyTemple is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SkyTemple.  If not, see <https://www.gnu.org/licenses/>.
import os
import re
import threading
from functools import partial
from typing import Tuple, List, Optional, TYPE_CHECKING

from gi.repository import Gtk, GtkSource, GLib, Gdk
from gi.repository.GtkSource import StyleSchemeManager, LanguageManager

from skytemple_files.common.ppmdu_config.data import Pmd2Data
from skytemple_ssb_debugger.model.breakpoint_manager import BreakpointManager
from skytemple_ssb_debugger.model.completion.calltips.calltip_emitter import CalltipEmitter
from skytemple_ssb_debugger.model.completion.constants import GtkSourceCompletionSsbConstants
from skytemple_ssb_debugger.model.completion.functions import GtkSourceCompletionSsbFunctions
from skytemple_ssb_debugger.model.ssb_files.file import SsbLoadedFile
from skytemple_ssb_debugger.pixbuf.breakpoint_icon import create_breakpoint_icon

if TYPE_CHECKING:
    from skytemple_ssb_debugger.controller.code_editor import CodeEditorController

MARK_PATTERN = re.compile('routine_(\\d+)_opcode_(\\d+)')
MARK_PATTERN_TMP = re.compile('TMP_routine_(\\d+)_opcode_(\\d+)')


class SSBEditorController:
    def __init__(
            self, parent: 'CodeEditorController', breakpoint_manager: BreakpointManager,
            ssb_file: SsbLoadedFile, rom_data: Pmd2Data, modified_handler
    ):
        path = os.path.abspath(os.path.dirname(__file__))
        self.builder = Gtk.Builder()
        self.builder.add_from_file(os.path.join(path, "ssb_editor.glade"))
        self.breakpoint_manager = breakpoint_manager
        self.rom_data = rom_data
        self.parent = parent
        self._modified_handler = modified_handler

        self._ssb = ssb_file
        # TODO: Show warnings depending on the current ssb file state ( not breakable )

        self._root: Gtk.Box = self.builder.get_object('code_editor')
        self._ssm = StyleSchemeManager()
        # TODO: Configurable?
        self._active_scheme: GtkSource.StyleScheme = self._ssm.get_scheme('builder-dark')
        self._lm = LanguageManager()
        self._lm.set_search_path(self._lm.get_search_path() + [os.path.join(path, '..')])

        # If True, the ExplorerScript view should be editable but not the SSBScript view
        # If False, the other way around.
        # TODO
        self._explorerscript_active = False
        self._waiting_for_reload = False

        self._ssb_script_view: GtkSource.View = None
        self._explorerscript_view: GtkSource.View = None
        self._ssb_script_revealer: Gtk.Revealer = None
        self._explorerscript_revealer: Gtk.Revealer = None
        self._ssb_script_search: Gtk.SearchEntry = None
        self._explorerscript_revealer: Gtk.SearchEntry = None
        self._ssb_script_search_context: GtkSource.SearchContext = None
        self._explorerscript_search_context: GtkSource.SearchContext = None

        self._loaded_search_window: Optional[Gtk.Dialog] = None
        self._active_search_context: Optional[GtkSource.SearchContext] = None

        self._mrk_attrs__breakpoint: GtkSource.MarkAttributes = GtkSource.MarkAttributes.new()
        self._mrk_attrs__breakpoint.set_background(self._mix_breakpoint_colors())
        self._mrk_attrs__breakpoint.set_pixbuf(create_breakpoint_icon())

        self.load_views(
            self.builder.get_object('page_ssbscript'), self.builder.get_object('page_explorerscript')
        )

        self.builder.connect_signals(self)
        self._ssb.register_reload_event_editor(self.on_ssb_reload)
        self._ssb.register_property_callback(self.on_ssb_property_change)
        self.on_ssb_property_change()

        self._root.show_all()

    def get_root_object(self) -> Gtk.Box:
        return self._root

    def destroy(self):
        self._ssb.unregister_reload_event_editor(self.on_ssb_reload)
        self._ssb.unregister_property_callback(self.on_ssb_property_change)
        self._root.destroy()

    @property
    def has_changes(self):
        if self._ssb_script_view and self._ssb_script_view.get_buffer().get_modified():
            return True
        if self._explorerscript_view and self._explorerscript_view.get_buffer().get_modified():
            return True
        return False

    @property
    def filename(self):
        return self._ssb.filename

    def save(self):
        """
        Save the SSB file. As a constraint only the ssbs or exps views should be editable, so in theory only one can
        have changes... - we save that one!
        """
        print(f"{self.filename}: Save")
        saved = False
        ready_to_reload = False
        modified_buffer = None
        if self._ssb_script_view.get_buffer().get_modified():
            modified_buffer: GtkSource.Buffer = self._ssb_script_view.get_buffer()
            ready_to_reload = self._ssb.file_manager.save_from_ssb_script(
                self._ssb.filename, modified_buffer.props.text
            )
            saved = True
            self._ssb_script_view.get_buffer().set_modified(False)
        if self._explorerscript_view.get_buffer().get_modified():
            modified_buffer: GtkSource.Buffer = self._explorerscript_view.get_buffer()
            ready_to_reload = self._ssb.file_manager.save_from_explorerscript(
                self._ssb.filename, modified_buffer.props.text
            )
            saved = True
            self._explorerscript_view.get_buffer().set_modified(False)
        if saved:
            self._waiting_for_reload = True
            # Build temporary text marks for the new source map. We will replace
            # the real ones with those in on_ssb_reloaded
            for model, view in [(self._ssb.ssbs, self._ssb_script_view), (self._ssb.exps, self._explorerscript_view)]:
                buffer: Gtk.TextBuffer = view.get_buffer()
                for routine_id, opcode_offset, line, column in model.source_map:
                    textiter = buffer.get_iter_at_line_offset(line, column)
                    buffer.create_mark(f'TMP_routine_{routine_id}_opcode_{opcode_offset}', textiter)
                    print(f'TMP_routine_{routine_id}_opcode_{opcode_offset}', '@', line - 1, ',', column)

            buffer = self._ssb_script_view.get_buffer()
            if self._explorerscript_active:
                buffer = self._explorerscript_view.get_buffer()
            # Resync the breakpoints at the Breakpoint Manager.
            # Collect all line marks and check which is the first TMP_routine text mark in it, this is
            # the opcode to break on.
            breakpoints_to_resync = []
            for line in range(0, modified_buffer.get_line_count()):
                marks = modified_buffer.get_source_marks_at_line(line, 'breakpoint')
                if len(marks) > 0:
                    routine_id_and_opcode_offset = self._get_opcode_in_line(buffer, line)
                    if routine_id_and_opcode_offset is None:
                        continue

                    breakpoints_to_resync.append(list(routine_id_and_opcode_offset))
            self.breakpoint_manager.resync(self._ssb.filename, breakpoints_to_resync)

            # If the file manager told us, then we can immediately trigger the SSB reloading,
            # this will trigger self.on_ssb_reloaded.
            if ready_to_reload:
                self._ssb.file_manager.force_reload(self._ssb.filename)

    def load_views(self, ssbs_bx: Gtk.Box, exps_bx: Gtk.Box):
        self._activate_spinner(ssbs_bx)
        self._activate_spinner(exps_bx)

        (ssbs_ovl, self._ssb_script_view, self._ssb_script_revealer,
         self._ssb_script_search, self._ssb_script_search_context) = self._create_editor()
        (exps_ovl, self._explorerscript_view, self._explorerscript_revealer,
         self._explorerscript_search, self._explorerscript_search_context) = self._create_editor()

        self._load_ssbs_completion()
        self._load_explorerscript_completion()

        self._update_view_editable_state()

        def process_loaded(bx, ovl, model, view, language):
            for child in bx.get_children():
                bx.remove(child)
            buffer: GtkSource.Buffer = view.get_buffer()
            buffer.set_text(model.text)
            buffer.set_modified(False)
            buffer.set_language(self._lm.get_language(language))
            buffer.set_highlight_syntax(True)

            for routine_id, opcode_offset, line, column in model.source_map:
                textiter = buffer.get_iter_at_line_offset(line, column)
                buffer.create_mark(f'routine_{routine_id}_opcode_{opcode_offset}', textiter)
                print(f'routine_{routine_id}_opcode_{opcode_offset}', '@', line - 1, ',', column)

            bx.pack_start(ovl, True, True, 0)

        def load_thread():
            # SSBSLoad
            self._ssb.ssbs.load()
            GLib.idle_add(partial(
                process_loaded, ssbs_bx, ssbs_ovl, self._ssb.ssbs, self._ssb_script_view, 'ssbs'
            ))

            self._ssb.exps.load()
            GLib.idle_add(partial(
                process_loaded, exps_bx, exps_ovl, self._ssb.exps, self._explorerscript_view, 'exps'
            ))
            GLib.idle_add(self._load_breakpoints_from_manager)

        threading.Thread(target=load_thread).start()

    def add_breakpoint(self, line_number: int, view: GtkSource.View):
        # This workflow is a bit complicacted, here's an outline.
        # TODO: Make proper documentation for this and also file loading / compilation / decompilation process.
        # - Update breakpoints now at manager using marks in SourceView
        #   - Using the old mapping from the source marks for now (if still loaded in ground engine)!
        #   - Write new tempoary text marks from source map (if still loaded in ground engine)
        #   - Re-sync breakpoint lines with opcode offsets of saved file at BreakpointManager (keeps it tmp for now,
        #     if still loaded in ground engine)
        # - On save, write new source map
        #   - [if SSBScript: SSBScript only, because no ExplorerScript generated yet, if ES fully regenerate SSBScript]
        # - If the file is no longer loaded in Ground Engine:
        #    - Switch over temporary source marks to new active source marks
        #    - Resync and activate new breakpoint positions
        # - If the file is loaded in Ground Engine, keep old source mapping [text marks!] and
        #   use that for breaking for now
        # - If the file was closed and the old text marks are no longer available, disable
        #   debugging for that file until reload [show warning before close and on open again]
        # - Save SSB file hashes to ground state file, do previous for all changed ssb files
        #   [show warning for affected files].

        # Unrelated note for ES:
        # - Generate in a separate, specified directory:
        #   - .exps, .hash, .expssm

        buffer: Gtk.TextBuffer = view.get_buffer()
        opcode_offset = self._get_opcode_in_line(buffer, line_number - 1)
        if opcode_offset is None:
            return
        routine_id = opcode_offset[0]
        opcode_offset = opcode_offset[1]

        self.breakpoint_manager.add(self._ssb.filename, routine_id, opcode_offset)

    def remove_breakpoint(self, mark: GtkSource.Mark):
        match = MARK_PATTERN.match(mark.get_name()[4:])
        routine_id, opcode_offset = match.group(1), match.group(2)

        self.breakpoint_manager.remove(self._ssb.filename, routine_id, opcode_offset)

    def on_breakpoint_added(self, routine_id, opcode_offset):
        print(f"{self.filename}: On breakpoint added")
        view: GtkSource.View
        for view in (self._ssb_script_view, self._explorerscript_view):
            buffer: GtkSource.Buffer = view.get_buffer()
            m: Gtk.TextMark = buffer.get_mark(f'routine_{routine_id}_opcode_{opcode_offset}')
            # TODO: proper logging and warnings!
            if m is None:
                print(f"WARNING: Mark not found routine_{routine_id}_opcode_{opcode_offset}.")
                continue
            line_iter = buffer.get_iter_at_line(buffer.get_iter_at_mark(m).get_line())
            lm: Gtk.TextMark = buffer.get_mark(f'for:routine_{routine_id}_opcode_{opcode_offset}')
            if lm is not None:
                print(f"WARNING: Line mark already found for:routine_{routine_id}_opcode_{opcode_offset}.")
                continue
            buffer.create_source_mark(f'for:routine_{routine_id}_opcode_{opcode_offset}', 'breakpoint', line_iter)

    def on_breakpoint_removed(self, routine_id, opcode_offset):
        view: GtkSource.View
        for view in (self._ssb_script_view, self._explorerscript_view):
            buffer: GtkSource.Buffer = view.get_buffer()
            m: Gtk.TextMark = buffer.get_mark(f'for:routine_{routine_id}_opcode_{opcode_offset}')
            if m is None:
                continue
            buffer.remove_source_marks(buffer.get_iter_at_mark(m), buffer.get_iter_at_mark(m))

    # Signal & event handlers
    def on_ssb_reload(self, ssb):
        """
        The ssb file is clear for reload (saved & no longer loaded in Ground Engine).
        Delete all breakpoint line marks and all regular text marks.
        If we have temporary text marks:
            Move the temporary text marks to be the new regular ones.
        """
        assert self.filename == ssb.filename
        print(f"{self.filename}: On reload")
        for view in (self._ssb_script_view, self._explorerscript_view):
            buffer: GtkSource.Buffer = view.get_buffer()
            # Remove all breakpoints
            buffer.remove_source_marks(buffer.get_start_iter(), buffer.get_end_iter(), 'breakpoint')

            # Remove all regular text marks and rename temporary
            # Only do this, if we are actively waiting for a reload, because only then, the breakpoint markers exist.
            if self._waiting_for_reload:
                textiter: Gtk.TextIter = buffer.get_start_iter().copy()
                # TODO: This is probably pretty slow
                while textiter.forward_char():
                    old_marks_at_pos = [
                        m for m in textiter.get_marks() if m.get_name() and m.get_name().startswith('routine_')
                    ]
                    new_marks_at_pos = [
                        m for m in textiter.get_marks() if m.get_name() and m.get_name().startswith('TMP_routine_')
                    ]
                    for m in old_marks_at_pos:
                        buffer.delete_mark(m)
                    for m in new_marks_at_pos:
                        name = m.get_name()
                        # Maybe by chance an old mark with this name still exists elsewhere, remove it.
                        om = buffer.get_mark(name[4:])
                        if om is not None:
                            buffer.delete_mark(om)
                        # Move by deleting and re-creating.
                        match = MARK_PATTERN_TMP.match(m.get_name())
                        buffer.create_mark(f'routine_{int(match.group(1))}_opcode_{int(match.group(2))}', textiter)
                        buffer.delete_mark(m)

            # Re-add all breakpoints:
            for rtn_id, opcode_offset in self.breakpoint_manager.saved_in_rom_get_for(self._ssb.filename):
                self.on_breakpoint_added(rtn_id, opcode_offset)

    def on_ssb_property_change(self, *args):
        """Fully rebuild the active info bar message based on the current state of the SSB."""
        info_bar: Gtk.InfoBar = self.builder.get_object('code_editor_box_ssbscript_bar')
        if self._explorerscript_active:
            info_bar: Gtk.InfoBar = self.builder.get_object('code_editor_box_es_bar')

        if self._ssb.not_breakable:
            self._refill_info_bar(
                info_bar, Gtk.MessageType.WARNING,
                "An old version of this script is still loaded in RAM, but breakpoints are not available.\n"
                "Debugging is disabled for this file, until it is reloaded."
            )
            return

        if not self._ssb.ram_state_up_to_date:
            self._refill_info_bar(
                info_bar, Gtk.MessageType.INFO,
                "An old version of this script is still loaded in RAM, old breakpoints are still used, until "
                "the file is reloaded."
            )
            return

        info_bar.set_message_type(Gtk.MessageType.OTHER)
        info_bar.set_revealed(False)

    def on_sourceview_line_mark_activated(self, widget: GtkSource.View, textiter: Gtk.TextIter, event: Gdk.Event):
        marks = widget.get_buffer().get_source_marks_at_iter(textiter)

        # Only allow editing one view.
        if self._explorerscript_active and widget == self._ssb_script_view:
            return
        if not self._explorerscript_active and widget == self._explorerscript_view:
            return

        # No mark? Add!
        if len(marks) < 1:
            self.add_breakpoint(textiter.get_line() + 1, widget)
        else:
            # Mark? Remove breakpoint!
            mark = marks[0]
            self.remove_breakpoint(mark)
        return True

    def on_sourcebuffer_delete_range(self, buffer: GtkSource.Buffer, start: Gtk.TextIter, end: Gtk.TextIter):
        if start.get_line() != end.get_line() or start.get_chars_in_line() == 0:
            i = start.copy()
            ms = []
            while i.get_offset() <= end.get_offset():
                ms += buffer.get_source_marks_at_iter(i, 'breakpoint')
                if not i.forward_char():
                    break
            for m in ms:
                self.remove_breakpoint(m)
        return True

    def on_sourceview_key_press_event(self, widget: Gtk.Widget, event: Gdk.EventKey):
        """Handle keyboard shortcuts"""
        # TODO: Move all of these to accelerators via main! Save is already moved there.
        if event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval == Gdk.KEY_f:
            # SEARCH
            revealer = self._explorerscript_revealer
            search = self._explorerscript_search
            if widget == self._ssb_script_view:
                revealer = self._ssb_script_revealer
                search = self._ssb_script_search
            revealer.set_reveal_child(True)
            search.grab_focus()
        elif event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval == Gdk.KEY_h:
            # REPLACE
            if not self._loaded_search_window:
                self._active_search_context = self._explorerscript_search_context
                if widget == self._ssb_script_view:
                    self._active_search_context = self._ssb_script_search_context
                search_settings: GtkSource.SearchSettings = self._active_search_context.get_settings()
                self._loaded_search_window: Gtk.Dialog = self.builder.get_object('sr_dialog')
                self.builder.get_object('sr_search_setting_case_sensitive').set_active(search_settings.get_case_sensitive())
                self.builder.get_object('sr_search_setting_match_words').set_active(search_settings.get_at_word_boundaries())
                self.builder.get_object('sr_search_setting_regex').set_active(search_settings.get_regex_enabled())
                self.builder.get_object('sr_search_setting_wrap_around').set_active(search_settings.get_wrap_around())
                self._loaded_search_window.set_title(f'Search and Replace in {self._ssb.filename}')
                self._loaded_search_window.show_all()
        return False

    def on_search_entry_focus_out_event(self, widget: Gtk.SearchEntry, *args):
        view = self._explorerscript_view
        revealer = self._explorerscript_revealer
        if widget == self._ssb_script_search:
            view = self._ssb_script_view
            revealer = self._ssb_script_revealer
        revealer.set_reveal_child(False)
        view.grab_focus()

    def on_search_entry_search_changed(self, widget: Gtk.SearchEntry):
        view = self._explorerscript_view
        context = self._explorerscript_search_context
        if widget == self._ssb_script_search:
            view = self._ssb_script_view
            context = self._ssb_script_search_context
        buffer: Gtk.TextBuffer = view.get_buffer()

        settings: GtkSource.SearchSettings = context.get_settings()
        settings.set_search_text(widget.get_text())
        found, match_start, match_end, wrap = context.forward(buffer.get_iter_at_offset(buffer.props.cursor_position))
        if found:
            buffer.select_range(match_start, match_end)

    def on_search_up_button_clicked(self, widget: Gtk.Button, search: Gtk.SearchEntry):
        view = self._explorerscript_view
        context = self._explorerscript_search_context
        if search == self._ssb_script_search:
            view = self._ssb_script_view
            context = self._ssb_script_search_context
        buffer: Gtk.TextBuffer = view.get_buffer()

        settings: GtkSource.SearchSettings = context.get_settings()
        settings.set_search_text(search.get_text())
        found, match_start, match_end, wrap = context.backward(buffer.get_iter_at_offset(buffer.props.cursor_position))
        if found:
            buffer.select_range(match_start, match_end)

    def on_search_down_button_clicked(self, widget: Gtk.Button, search: Gtk.SearchEntry):
        view = self._explorerscript_view
        context = self._explorerscript_search_context
        if search == self._ssb_script_search:
            view = self._ssb_script_view
            context = self._ssb_script_search_context
        buffer: Gtk.TextBuffer = view.get_buffer()

        settings: GtkSource.SearchSettings = context.get_settings()
        settings.set_search_text(search.get_text())
        cursor = buffer.get_iter_at_offset(buffer.props.cursor_position)
        found, match_start, match_end, wrap = context.forward(cursor)
        if found:
            if match_start.get_offset() == cursor.get_offset():
                # Repeat once, to really get down
                found, match_start, match_end, wrap = context.forward(match_end)
            if found:
                buffer.select_range(match_start, match_end)

    def on_sr_dialog_close(self, dialog: Gtk.Dialog, *args):
        self._loaded_search_window = None
        dialog.hide()
        return True

    def on_sr_search_setting_regex_toggled(self, btn: Gtk.CheckButton, *args):
        s: GtkSource.SearchSettings = self._active_search_context.get_settings()
        s.set_regex_enabled(btn.get_active())

    def on_sr_search_setting_wrap_around_toggled(self, btn: Gtk.CheckButton, *args):
        s: GtkSource.SearchSettings = self._active_search_context.get_settings()
        s.set_wrap_around(btn.get_active())

    def on_sr_search_setting_match_words_toggled(self, btn: Gtk.CheckButton, *args):
        s: GtkSource.SearchSettings = self._active_search_context.get_settings()
        s.set_at_word_boundaries(btn.get_active())

    def on_sr_search_setting_case_sensitive_toggled(self, btn: Gtk.CheckButton, *args):
        s: GtkSource.SearchSettings = self._active_search_context.get_settings()
        s.set_case_sensitive(btn.get_active())

    def on_sr_search_clicked(self, btn: Gtk.Button, *args):
        buffer: Gtk.TextBuffer = self._active_search_context.get_buffer()
        settings: GtkSource.SearchSettings = self._active_search_context.get_settings()

        settings.set_search_text(self.builder.get_object('sr_search_text').get_text())
        cursor = buffer.get_iter_at_offset(buffer.props.cursor_position)
        search_down = not self.builder.get_object('sr_search_setting_search_backwards').get_active()
        if search_down:
            found, match_start, match_end, wrap = self._active_search_context.forward(cursor)
        else:
            found, match_start, match_end, wrap = self._active_search_context.backward(cursor)
        if found:
            if search_down and match_start.get_offset() == cursor.get_offset():
                # Repeat once, to really get down
                found, match_start, match_end, wrap = self._active_search_context.forward(match_end)
            buffer.select_range(match_start, match_end)

    def on_sr_replace_clicked(self, btn: Gtk.Button, *args):
        buffer: Gtk.TextBuffer = self._active_search_context.get_buffer()
        settings: GtkSource.SearchSettings = self._active_search_context.get_settings()

        settings.set_search_text(self.builder.get_object('sr_search_text').get_text())
        cursor = buffer.get_iter_at_offset(buffer.props.cursor_position)
        search_down = not self.builder.get_object('sr_search_setting_search_backwards').get_active()
        if search_down:
            found, match_start, match_end, wrap = self._active_search_context.forward(cursor)
        else:
            found, match_start, match_end, wrap = self._active_search_context.backward(cursor)
        if found:
            # No running twice this time, because if we search forward we take the current pos.
            self._active_search_context.replace(match_start, match_end, self.builder.get_object('sr_replace_text').get_text(), -1)

    def on_sr_replace_all_clicked(self, btn: Gtk.Button, *args):
        settings: GtkSource.SearchSettings = self._active_search_context.get_settings()

        settings.set_search_text(self.builder.get_object('sr_search_text').get_text())
        self._active_search_context.replace_all(self.builder.get_object('sr_replace_text').get_text(), -1)

    def on_text_buffer_modified(self, buffer: Gtk.TextBuffer, *args):
        if self._modified_handler:
            self._modified_handler(self, buffer.get_modified())

    # Utility

    def _load_breakpoints_from_manager(self):
        for routine_id, opcode_offset in self.breakpoint_manager.saved_in_rom_get_for(self._ssb.filename):
            self.on_breakpoint_added(routine_id, opcode_offset)

    def _mix_breakpoint_colors(self):
        """Mix the default background color with the error color to get a nice breakpoint bg color"""
        breakpoint_bg = color_hex_to_rgb(self._active_scheme.get_style('def:error').props.background, 51)
        text_bg = color_hex_to_rgb(self._active_scheme.get_style('text').props.background, 204)
        return Gdk.RGBA(*get_mixed_color(
            breakpoint_bg, text_bg
        ))

    def _create_editor(self) -> Tuple[Gtk.ScrolledWindow, GtkSource.View, Gtk.Revealer, Gtk.SearchEntry, GtkSource.SearchContext]:
        ovl: Gtk.Overlay = Gtk.Overlay.new()
        sw: Gtk.ScrolledWindow = Gtk.ScrolledWindow.new()
        view: GtkSource.View = GtkSource.View.new()

        view.set_mark_attributes('breakpoint', self._mrk_attrs__breakpoint, 1)

        buffer: GtkSource.Buffer = view.get_buffer()
        view.set_show_line_numbers(True)
        view.set_show_line_marks(True)
        view.set_auto_indent(True)
        view.set_insert_spaces_instead_of_tabs(True)
        view.set_indent_width(4)
        view.set_show_right_margin(True)
        view.set_indent_on_tab(True)
        view.set_highlight_current_line(True)
        view.set_smart_backspace(True)
        view.set_smart_home_end(True)
        view.set_monospace(True)
        buffer.set_highlight_matching_brackets(True)
        buffer.set_style_scheme(self._active_scheme)

        view.connect("line-mark-activated", self.on_sourceview_line_mark_activated)
        view.connect("key-press-event", self.on_sourceview_key_press_event)
        buffer.connect("delete-range", self.on_sourcebuffer_delete_range)

        # TODO: One of the two vies should not be editable!
        buffer.connect("modified-changed", self.on_text_buffer_modified)

        sw.add(view)
        ovl.add(sw)

        # SEARCH
        rvlr: Gtk.Revealer = Gtk.Revealer.new()
        rvlr.get_style_context().add_class('backdrop')
        rvlr.set_valign(Gtk.Align.START)
        rvlr.set_halign(Gtk.Align.END)
        search_frame: Gtk.Frame = Gtk.Frame.new()
        search_frame.set_margin_top(2)
        search_frame.set_margin_bottom(2)
        search_frame.set_margin_left(2)
        search_frame.set_margin_right(2)
        hbox: Gtk.Box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        search: Gtk.SearchEntry = Gtk.SearchEntry.new()
        search.set_size_request(260, -1)
        up_button: Gtk.Button = Gtk.Button.new_from_icon_name('go-up-symbolic', Gtk.IconSize.BUTTON)
        up_button.set_can_focus(False)
        down_button: Gtk.Button = Gtk.Button.new_from_icon_name('go-down-symbolic', Gtk.IconSize.BUTTON)
        down_button.set_can_focus(False)

        search.connect('search-changed', self.on_search_entry_search_changed)
        search.connect('focus-out-event', self.on_search_entry_focus_out_event)
        up_button.connect('clicked', self.on_search_up_button_clicked, search)
        down_button.connect('clicked', self.on_search_down_button_clicked, search)

        hbox.pack_start(search, False, True, 0)
        hbox.pack_start(up_button, False, True, 0)
        hbox.pack_start(down_button, False, True, 0)
        search_frame.add(hbox)
        rvlr.add(search_frame)
        ovl.add_overlay(rvlr)

        search_context = GtkSource.SearchContext.new(buffer)
        search_context.get_settings().set_wrap_around(True)
        # END SEARCH

        ovl.show_all()
        return ovl, view, rvlr, search, search_context

    @staticmethod
    def _activate_spinner(bx):
        spinner: Gtk.Spinner = Gtk.Spinner.new()
        spinner.show()
        spinner.start()
        bx.pack_start(spinner, True, False, 0)

    @staticmethod
    def _get_opcode_in_line(buffer: Gtk.TextBuffer, line_number_0_indexed, use_temp_markers=False) -> Optional[Tuple[int, int]]:
        marker_prefix = 'routine_'
        marker_pattern = MARK_PATTERN
        if use_temp_markers:
            marker_prefix = 'TMP_routine_'
            marker_pattern = MARK_PATTERN_TMP
        i = buffer.get_iter_at_line(line_number_0_indexed)
        while i.get_line() == line_number_0_indexed:
            marks_at_pos = [
                m for m in i.get_marks() if m.get_name() and m.get_name().startswith(marker_prefix)
            ]
            if len(marks_at_pos) > 0:
                match = marker_pattern.match(marks_at_pos[0].get_name())
                return int(match.group(1)), int(match.group(2))
            if not i.forward_char():  # TODO: the other forwards might also work!
                return None
        return None

    def _load_ssbs_completion(self):
        view = self._ssb_script_view
        completion: GtkSource.Completion = view.get_completion()

        completion.add_provider(GtkSourceCompletionSsbConstants(self.rom_data))
        completion.add_provider(GtkSourceCompletionSsbFunctions(self.rom_data.script_data.op_codes))
        CalltipEmitter(self._ssb_script_view, self.rom_data.script_data.op_codes)

    def _load_explorerscript_completion(self):
        pass  # todo

    def _update_view_editable_state(self):
        """Update which view is editable based on self._explorerscript_active"""
        if self._explorerscript_active:
            # Enable ES editing
            self._explorerscript_view.set_editable(True)
            # Disable SSBS editing
            self._ssb_script_view.set_editable(False)
            # Show notice on SSBS info bar
            self._refill_info_bar(
                self.builder.get_object('code_editor_box_ssbscript_bar'), Gtk.MessageType.INFO,
                "This is a read-only representation of the compiled ExplorerScript."
            )
            # TODO: Button to reset ExplorerScript.
            # Force refresh of ES info bar
            self.on_ssb_property_change()
        else:
            # Enable SSBS editing
            self._ssb_script_view.set_editable(True)
            # Disable ES editing
            self._explorerscript_view.set_editable(False)
            # Show notice on SSBS info bar
            self._refill_info_bar(
                self.builder.get_object('code_editor_box_es_bar'), Gtk.MessageType.INFO,
                "ExplorerScript is not avaiable for this file."
            )
            # Force refresh of ES info bar
            self.on_ssb_property_change()

    def _refill_info_bar(self, info_bar: Gtk.InfoBar, message_type: Gtk.MessageType, text: str):
        info_bar.set_message_type(message_type)
        content: Gtk.Box = info_bar.get_content_area()
        for c in content.get_children():
            content.remove(c)
        lbl: Gtk.Label = Gtk.Label.new(text)
        lbl.set_line_wrap(True)
        content.add(lbl)
        info_bar.set_revealed(True)
        info_bar.show_all()


def get_mixed_color(color_rgba1, color_rgba2):
    red   = (color_rgba1[0] * (255 - color_rgba2[3]) + color_rgba2[0] * color_rgba2[3]) / 255
    green = (color_rgba1[1] * (255 - color_rgba2[3]) + color_rgba2[1] * color_rgba2[3]) / 255
    blue  = (color_rgba1[2] * (255 - color_rgba2[3]) + color_rgba2[2] * color_rgba2[3]) / 255
    return int(red) / 255, int(green) / 255, int(blue) / 255, 1.0


def color_hex_to_rgb(hexx, alpha):
    return tuple(int(hexx.lstrip('#')[i:i+2], 16) for i in (0, 2, 4)) + (alpha,)