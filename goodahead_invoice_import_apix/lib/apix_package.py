# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""One downloaded APIX package, unpacked. Pure Python: no
import from the ERP at all. The Finvoice sniff (``looks_like_finvoice`` of the
Finvoice add-on) is passed in by the caller as ``sniff``, so this module can be
loaded and tested without the ERP (tests/test_apix_package_pure.py).

APIX Download "returns contents of the whole storage with attachments": a ZIP
holding the invoice XML, the invoice image (PDF) and any attachments. The
listing names the first two (``DocumentName``, ``ImageName``). A body that is
not a ZIP but a Finvoice XML is taken as the Finvoice file itself.

`unpack` never decides about the invoice: it only says which file is the
Finvoice message and which the image, and keeps every file so nothing of the
package is lost.
"""

import io
import mimetypes
import os
import zipfile
from dataclasses import dataclass, field

#: Unpacked size above which a package is refused (a ZIP bomb, or something
#: that is not an invoice package). Real packages are a few megabytes.
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 500

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
XML_MIMETYPE = "application/xml"
PDF_MIMETYPE = "application/pdf"

#: `PackageError.code` values.
NOT_A_PACKAGE = "apix.package_unreadable"  # final: the content is not a package
TRUNCATED = "apix.package_truncated"  # technical: a ZIP that does not open


class PackageError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PackageFile:
    name: str
    content: bytes
    mimetype: str


@dataclass
class Package:
    """``files`` in package order; ``finvoice`` / ``image`` index into it
    (None when absent)."""

    files: list = field(default_factory=list)
    finvoice: int = None
    image: int = None

    @property
    def finvoice_file(self):
        return None if self.finvoice is None else self.files[self.finvoice]

    @property
    def image_file(self):
        return None if self.image is None else self.files[self.image]

    def other_files(self):
        return [f for i, f in enumerate(self.files) if i not in (self.finvoice, self.image)]


def mimetype_of(name, content, sniff):
    """``sniff(content) -> bool``: whether ``content`` is a Finvoice message."""
    if content[:5] == PDF_MAGIC:
        return PDF_MIMETYPE
    if sniff(content):
        return XML_MIMETYPE
    guessed, _encoding = mimetypes.guess_type(name or "")
    return guessed or "application/octet-stream"


def is_zip(body):
    return bytes(body[:4]) == ZIP_MAGIC


def unpack(body, document_name=None, image_name=None, *, sniff):
    """``body`` (the Download answer) -> `Package`. Raises `PackageError`.

    ``sniff(content) -> bool`` tells a Finvoice message apart (the model
    passes the Finvoice add-on's ``looks_like_finvoice``)."""
    body = bytes(body or b"")
    if is_zip(body):
        files = _zip_members(body, sniff)
    elif sniff(body):
        name = os.path.basename(document_name or "") or "finvoice.xml"
        files = [PackageFile(name, body, XML_MIMETYPE)]
    else:
        raise PackageError(
            NOT_A_PACKAGE,
            "The APIX package is neither a ZIP file nor a Finvoice message.",
        )
    package = Package(files=files)
    package.finvoice = _find_finvoice(files, document_name, sniff)
    package.image = _find_image(files, image_name, package.finvoice)
    return package


def _zip_members(body, sniff):
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > MAX_MEMBERS:
            raise PackageError(NOT_A_PACKAGE, f"The APIX package holds {len(infos)} files.")
        if sum(info.file_size for info in infos) > MAX_UNPACKED_BYTES:
            raise PackageError(NOT_A_PACKAGE, "The APIX package is too large to unpack.")
        files = []
        for info in infos:
            name = os.path.basename(info.filename.replace("\\", "/"))
            if not name or info.filename.startswith("__MACOSX/"):
                continue
            content = archive.read(info)
            files.append(PackageFile(name, content, mimetype_of(name, content, sniff)))
    except PackageError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, ValueError) as exc:
        # Starts like a ZIP but does not open: most likely cut off on the way.
        raise PackageError(
            TRUNCATED, f"The APIX package could not be opened ({type(exc).__name__})."
        ) from None
    if not files:
        raise PackageError(NOT_A_PACKAGE, "The APIX package is empty.")
    return files


def _same_name(name, wanted):
    return bool(wanted) and name.casefold() == os.path.basename(wanted).casefold()


def _find_finvoice(files, document_name, sniff):
    """The listing's DocumentName when it names a file of the package, else
    the first file that looks like Finvoice."""
    for index, package_file in enumerate(files):
        if _same_name(package_file.name, document_name):
            return index
    for index, package_file in enumerate(files):
        if sniff(package_file.content):
            return index
    return None


def _find_image(files, image_name, finvoice_index):
    for index, package_file in enumerate(files):
        if index != finvoice_index and _same_name(package_file.name, image_name):
            return index
    for index, package_file in enumerate(files):
        if index != finvoice_index and package_file.mimetype == PDF_MIMETYPE:
            return index
    return None
