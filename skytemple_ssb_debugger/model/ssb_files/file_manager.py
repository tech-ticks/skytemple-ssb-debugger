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
import base64
import hashlib
from typing import Dict

from ndspy.rom import NintendoDSRom

from skytemple_files.common.types.file_types import FileType
from skytemple_ssb_debugger.model.ssb_files.file import SsbLoadedFile


class SsbFileManager:
    def __init__(self, rom: NintendoDSRom):
        self.rom = rom
        # TODO: Mechanism to close files again!
        self._open_files: Dict[str, SsbLoadedFile] = {}

    def get(self, filename: str) -> SsbLoadedFile:
        """Get a file. If loaded by editor or ground engine, use the open_* methods instead!"""
        if filename not in self._open_files:
            self._open_files[filename] = SsbLoadedFile(
                filename, FileType.SSB.deserialize(self.rom.getFileByName(filename)), self
            )
        return self._open_files[filename]

    def save_from_ssb_script(self, filename: str, code: str):
        """
        Save an SSB model from SSBScript. It's existing model and source map will be updated.
        If the file was not loaded in the ground engine, and is thus ready
        to reload for the editors, True is returned. You may call self.force_reload()
        when you are ready (to trigger ssb reload event).
        Otherwise False is returned and the event will be triggered later automatically.
        """
        pass  # todo
        # After save:
        return self._handle_after_save(filename)

    def save_from_explorerscript(self, filename: str, code: str):
        """
        Save an SSB model from ExplorerScript. It's existing model and source map will be updated.
        If the file was not loaded in the ground engine, and is thus ready
        to reload for the editors, True is returned. You may call self.force_reload()
        when you are ready (to trigger ssb reload event).
        Otherwise False is returned and the event will be triggered later automatically.
        """
        pass  # todo
        # After save:
        return self._handle_after_save(filename)

    def force_reload(self, filename: str):
        """
        Force a SSB reload event to be triggered. You MUST only call this after one of the save
        methods have returned True.
        """
        print(f"{filename}: Force reload")
        self._open_files[filename].signal_editor_reload()

    def open_in_editor(self, filename: str):
        self.get(filename)
        print(f"{filename}: Opened in editor")
        self._open_files[filename].opened_in_editor = True
        return self._open_files[filename]

    def open_in_ground_engine(self, filename: str):
        self.get(filename)
        print(f"{filename}: Opened in Ground Engine")
        self._open_files[filename].opened_in_ground_engine = True
        # The file was reloaded in RAM:
        if not self._open_files[filename].ram_state_up_to_date:
            self._open_files[filename].ram_state_up_to_date = True
            self._open_files[filename].not_breakable = False
            self._open_files[filename].signal_editor_reload()

        return self._open_files[filename]

    def close_in_editor(self, filename: str, warning_callback):
        """
        # - If the file was closed and the old text marks are no longer available, disable
        #   debugging for that file until reload [show warning before close]
        """
        if not self._open_files[filename].ram_state_up_to_date:
            if not warning_callback():
                return False
            self._open_files[filename].not_breakable = True
        print(f"{filename}: Closed in editor")
        self._open_files[filename].opened_in_editor = False
        return True

    def close_in_ground_engine(self, filename: str):
        """
        # - If the file is no longer loaded in Ground Engine: Regenerate text marks from source map.
        """
        self._open_files[filename].opened_in_ground_engine = False
        self._open_files[filename].not_breakable = False
        if not self._open_files[filename].ram_state_up_to_date:
            self._open_files[filename].ram_state_up_to_date = True
            self._open_files[filename].signal_editor_reload()
        print(f"{filename}: Closed in Ground Engine")
        pass

    def _handle_after_save(self, filename: str):
        """
        # - If the file is no longer loaded in Ground Engine: Regenerate text marks from source map.
        Returns whether a reload is possible.
        """
        self._open_files[filename].ram_state_up_to_date = False
        if not self._open_files[filename].opened_in_ground_engine:
            self._open_files[filename].ram_state_up_to_date = True
            print(f"{filename}: Can be reloaded")
            return True
        print(f"{filename}: Can NOT be reloaded")
        return False

    def hash_for(self, filename: str):
        self.get(filename)
        return base64.b64encode(hashlib.sha256(self._open_files[filename].ssb_model.original_binary_data))

    def mark_invalid(self, filename: str):
        """Mark a file as not breakable, because source mappings are not available."""
        self.get(filename)
        self._open_files[filename].ram_state_up_to_date = False
        self._open_files[filename].not_breakable = True