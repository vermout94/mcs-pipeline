"""Read access to the artifact measure-<correlation ID> (ZIP file).

The ZIP file is stored unchanged. This module reads its members in memory and
never extracts them to disk.
"""

import io
import json
import zipfile

MAX_MEMBER_BYTES = 50 * 1024 * 1024


class ArtifactError(Exception):
    pass


class Artifact:
    def __init__(self, data):
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as error:
            raise ArtifactError(f"not a ZIP file: {error}") from None
        self.names = sorted(info.filename for info in self._zip.infolist() if not info.is_dir())

    def has(self, name):
        return name in self.names

    def read(self, name):
        if name not in self.names:
            return None
        info = self._zip.getinfo(name)
        if info.file_size > MAX_MEMBER_BYTES:
            raise ArtifactError(f"{name} is larger than {MAX_MEMBER_BYTES} bytes")
        return self._zip.read(name)

    def text(self, name):
        data = self.read(name)
        return None if data is None else data.decode("utf-8", "replace")

    def json(self, name):
        data = self.read(name)
        if data is None:
            return None
        try:
            return json.loads(data)
        except ValueError as error:
            raise ArtifactError(f"{name}: {error}") from None
