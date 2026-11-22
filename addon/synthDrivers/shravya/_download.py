"""Download one resource archive, resuming only the same HTTP entity."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_LOG = logging.getLogger(__name__)
_BLOCK_SIZE = 64 * 1024


def _previous(part: Path, metadata: Path, url: str) -> tuple[int, str, int] | None:
	if not part.is_file() or not metadata.is_file():
		return None
	try:
		info = cast(dict[str, object], json.loads(metadata.read_text(encoding="utf-8")))
		etag, total = info["etag"], info["total"]
		offset = part.stat().st_size
		if (
			info["url"] == url
			and isinstance(etag, str)
			and etag.startswith('"')
			and etag.endswith('"')
			and isinstance(total, int)
			and not isinstance(total, bool)
			and 0 < offset < total
		):
			return offset, etag, total
	except (OSError, ValueError, KeyError, TypeError):
		_LOG.warning("Could not read the previous resource download metadata", exc_info=True)
		return None
	_LOG.warning("Previous resource download cannot be resumed; restarting")
	return None


def download(url: str, archive: Path, progress: Callable[[int, int], None]) -> None:
	archive.parent.mkdir(parents=True, exist_ok=True)
	part = archive.with_suffix(archive.suffix + ".part")
	metadata = archive.with_suffix(archive.suffix + ".json")
	previous = _previous(part, metadata, url)
	headers = {"User-Agent": "Shravya NVDA add-on"}
	if previous:
		offset, etag, _ = previous
		headers.update({"Range": f"bytes={offset}-", "If-Range": etag})
	try:
		response = urlopen(Request(url, headers=headers), timeout=30)
	except HTTPError as error:
		if not previous or error.code != 416:
			raise
		error.close()
		previous = None
		response = urlopen(Request(url, headers={"User-Agent": headers["User-Agent"]}), timeout=30)

	if response.status == 206 and previous:
		offset, etag, total = previous
		valid = (
			response.headers.get("ETag") == etag
			and response.headers.get("Content-Range") == f"bytes {offset}-{total - 1}/{total}"
			and response.headers.get("Content-Length") == str(total - offset)
		)
		if not valid:
			_LOG.warning("Resource server returned an invalid resumed response; restarting")
			response.close()
			previous = None
			response = urlopen(Request(url, headers={"User-Agent": headers["User-Agent"]}), timeout=30)

	with response:
		if response.status == 206:
			if previous is None:
				raise OSError("Unexpected partial resource response")
			offset, etag, total = previous
			mode = "ab"
		elif response.status == 200:
			length = response.headers.get("Content-Length", "")
			if not re.fullmatch(r"[0-9]+", length) or int(length) == 0:
				raise OSError("The resource server did not provide a valid content length")
			total = int(length)
			offset = 0
			etag = response.headers.get("ETag", "")
			mode = "wb"
		else:
			raise OSError(f"Unexpected resource response: {response.status}")

		with part.open(mode) as destination:
			if mode == "wb":
				if etag.startswith('"') and etag.endswith('"'):
					_ = metadata.write_text(
						json.dumps({"url": url, "etag": etag, "total": total}),
						encoding="utf-8",
					)
				else:
					metadata.unlink(missing_ok=True)
			progress(offset, total)
			while block := response.read(_BLOCK_SIZE):
				_ = destination.write(block)
				offset += len(block)
				if offset > total:
					raise OSError("The resource download exceeded its expected size")
				progress(offset, total)
			if offset != total:
				raise OSError(f"The resource download ended early ({offset} of {total} bytes)")
	_ = part.replace(archive)
	metadata.unlink(missing_ok=True)
