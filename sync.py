"""NAS의 이미지 파일을 FTP로 받아 로컬에 동일한 JPG 포맷으로 동기화."""
from __future__ import annotations

import argparse
import ftplib
import io
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # Python 3.10 이하

import rawpy
from PIL import Image, ImageOps, UnidentifiedImageError
from tqdm import tqdm

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    pillow_heif = None
    HEIC_SUPPORTED = False

# rawpy(LibRaw) 로 디코딩할 RAW 포맷
RAW_SUFFIXES = {
    ".nef", ".nrw",          # Nikon
    ".arw", ".srf", ".sr2",  # Sony
    ".cr2", ".cr3", ".crw",  # Canon
    ".dng",                  # Adobe / 범용
    ".orf",                  # Olympus
    ".rw2",                  # Panasonic
    ".raf",                  # Fujifilm
    ".pef",                  # Pentax
    ".srw",                  # Samsung
    ".x3f",                  # Sigma
    ".3fr",                  # Hasselblad
    ".kdc", ".dcr",          # Kodak
    ".mrw",                  # Minolta
    ".rwl",                  # Leica
}
# Pillow 로 디코딩할 일반 이미지 포맷
IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".jpe",
    ".png",
    ".tif", ".tiff",
    ".bmp",
    ".webp",
    ".heic", ".heif",
    ".gif",
}
SUPPORTED_SUFFIXES = RAW_SUFFIXES | IMAGE_SUFFIXES
HEIC_SUFFIXES = {".heic", ".heif"}

STATE_FILENAME = ".syncphoto-state.json"
STATE_VERSION = 1
LOG = logging.getLogger("syncphoto")


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    remote_root: PurePosixPath
    local_root: Path
    jpg_quality: int
    workers: int

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open("rb") as f:
            data = tomllib.load(f)
        ftp = data["ftp"]
        sync = data["sync"]
        return cls(
            host=ftp["host"],
            port=int(ftp.get("port", 21)),
            username=ftp.get("username", "anonymous"),
            password=ftp.get("password", ""),
            use_tls=bool(ftp.get("use_tls", False)),
            remote_root=PurePosixPath(sync["remote_root"]),
            local_root=Path(sync["local_root"]).expanduser(),
            jpg_quality=int(sync.get("jpg_quality", 88)),
            workers=int(sync.get("workers", 3)),
        )


@dataclass(frozen=True)
class RemoteFile:
    size: int
    mtime: int  # epoch seconds (UTC)


class State:
    """{remote_relpath: {size, mtime, jpg, completed_at}} 매니페스트."""

    def __init__(self, path: Path, entries: dict[str, dict[str, Any]]):
        self.path = path
        self.entries = entries
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls(path, {})
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            LOG.warning("상태 파일 읽기 실패, 새로 시작: %s", e)
            return cls(path, {})
        if data.get("version") != STATE_VERSION:
            LOG.warning("상태 파일 버전 불일치, 새로 시작")
            return cls(path, {})
        return cls(path, data.get("entries") or {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".part")
        payload = {"version": STATE_VERSION, "entries": self.entries}
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            "utf-8",
        )
        os.replace(tmp, self.path)

    def update(self, rel: str, size: int, mtime: int, jpg: str) -> None:
        with self._lock:
            self.entries[rel] = {
                "size": size,
                "mtime": mtime,
                "jpg": jpg,
                "completed_at": int(time.time()),
            }

    def remove(self, rel: str) -> dict[str, Any] | None:
        with self._lock:
            return self.entries.pop(rel, None)


def make_ftp(cfg: Config) -> ftplib.FTP:
    ftp: ftplib.FTP = ftplib.FTP_TLS(timeout=60) if cfg.use_tls else ftplib.FTP(timeout=60)
    ftp.encoding = "utf-8"
    ftp.connect(cfg.host, cfg.port)
    ftp.login(cfg.username, cfg.password)
    if isinstance(ftp, ftplib.FTP_TLS):
        ftp.prot_p()
    ftp.set_pasv(True)
    return ftp


def _parse_mlsd_modify(s: str) -> int:
    if not s:
        return 0
    s = s.split(".", 1)[0]  # 소수 초 제거
    try:
        dt = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


_SKIP_NAMES = {".DS_Store", "Thumbs.db"}


def _is_ignored_name(name: str) -> bool:
    if name in (".", "..") or not name:
        return True
    # macOS AppleDouble 사이드카, 숨김 메타파일은 무시
    return name.startswith("._") or name in _SKIP_NAMES


def _entry_as_image(name: str, facts: dict[str, str], full: PurePosixPath) -> RemoteFile | None:
    if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
        return None
    size = int(facts.get("size", 0))
    if size == 0:
        LOG.warning("빈 파일 건너뜀: %s", full)
        return None
    return RemoteFile(size=size, mtime=_parse_mlsd_modify(facts.get("modify", "")))


def _scan_directory(
    entries: list[tuple[str, dict[str, str]]],
    cur: PurePosixPath,
    root: PurePosixPath,
    result: dict[PurePosixPath, RemoteFile],
    stack: list[PurePosixPath],
    bar: tqdm,
) -> int:
    inspected = 0
    for name, facts in entries:
        if _is_ignored_name(name):
            continue
        inspected += 1
        bar.update(1)
        ftype = facts.get("type", "")
        full = cur / name
        if ftype == "dir":
            stack.append(full)
        elif ftype == "file":
            image = _entry_as_image(name, facts, full)
            if image is not None:
                result[full.relative_to(root)] = image
                bar.set_postfix(images=len(result), refresh=False)
    return inspected


def list_remote_images(
    ftp: ftplib.FTP, root: PurePosixPath
) -> dict[PurePosixPath, RemoteFile]:
    result: dict[PurePosixPath, RemoteFile] = {}
    stack = [root]
    inspected = 0
    dirs_scanned = 0
    with tqdm(desc="인덱싱", unit="개", leave=True) as bar:
        while stack:
            cur = stack.pop()
            dirs_scanned += 1
            try:
                entries = list(ftp.mlsd(str(cur), facts=["type", "size", "modify"]))
            except (ftplib.error_perm, ftplib.error_temp, OSError) as e:
                LOG.warning("디렉터리 읽기 실패: %s (%s)", cur, e)
                continue
            inspected += _scan_directory(entries, cur, root, result, stack, bar)
            bar.set_postfix(images=len(result), dirs=dirs_scanned, refresh=False)
    LOG.info(
        "인덱싱 완료: 디렉터리 %d개 / 검토 %d개 / 이미지 %d개",
        dirs_scanned,
        inspected,
        len(result),
    )
    return result


def src_to_jpg_relpath(rel: PurePosixPath) -> Path:
    return Path(*rel.parts).with_suffix(".jpg")


_JPEG_SUFFIXES = {".jpg", ".jpeg", ".jpe"}


def _collision_priority(rel: PurePosixPath) -> int:
    suffix = Path(rel.name).suffix.lower()
    if suffix in _JPEG_SUFFIXES:
        return 0  # 카메라가 만든 JPG 우선 (재인코딩만 하면 되므로 변환 비용 최저)
    if suffix in IMAGE_SUFFIXES:
        return 1  # 그 외 일반 이미지
    return 2  # RAW (가장 변환 비용이 크므로 최후 순위)


def resolve_collisions(
    remote: dict[PurePosixPath, RemoteFile],
) -> dict[PurePosixPath, RemoteFile]:
    """여러 원본이 같은 출력 경로로 매핑되면 JPG → 일반 이미지 → RAW 순으로 하나만 남김."""
    by_dest: dict[Path, list[PurePosixPath]] = {}
    for rel in remote:
        by_dest.setdefault(src_to_jpg_relpath(rel), []).append(rel)
    keep: dict[PurePosixPath, RemoteFile] = {}
    for sources in by_dest.values():
        if len(sources) == 1:
            keep[sources[0]] = remote[sources[0]]
            continue
        sources.sort(key=lambda s: (_collision_priority(s), str(s)))
        chosen = sources[0]
        keep[chosen] = remote[chosen]
        for excluded in sources[1:]:
            LOG.warning("이름 충돌로 제외: %s (선택: %s)", excluded, chosen)
    return keep


def _save_jpeg(img: Image.Image, dest: Path, quality: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    img.save(
        tmp,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=True,
        subsampling=2,
    )
    os.replace(tmp, dest)


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    """알파/팔레트 이미지를 흰 배경에 합성해 RGB로 변환."""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode == "P":
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def _convert_raw(image_bytes: bytes, dest: Path, quality: int) -> None:
    with rawpy.imread(io.BytesIO(image_bytes)) as raw:
        rgb = raw.postprocess(
            use_camera_wb=True,
            no_auto_bright=False,
            output_bps=8,
        )
    _save_jpeg(Image.fromarray(rgb), dest, quality)


def _convert_pillow(image_bytes: bytes, dest: Path, quality: int) -> None:
    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    img = ImageOps.exif_transpose(img) or img
    img = _flatten_to_rgb(img)
    _save_jpeg(img, dest, quality)


def convert_to_jpg(image_bytes: bytes, src_suffix: str, dest: Path, quality: int) -> None:
    if not image_bytes:
        raise ValueError("빈 파일")
    suffix = src_suffix.lower()
    if suffix in RAW_SUFFIXES:
        _convert_raw(image_bytes, dest, quality)
        return
    if suffix in HEIC_SUFFIXES and not HEIC_SUPPORTED:
        raise RuntimeError(
            "HEIC/HEIF 변환을 위해 pillow-heif를 설치하세요 (pip install pillow-heif)"
        )
    try:
        _convert_pillow(image_bytes, dest, quality)
    except UnidentifiedImageError as pil_err:
        # 확장자와 실제 내용이 다를 수 있음 (예: .tif로 저장된 RAW). rawpy로 한번 더 시도.
        try:
            _convert_raw(image_bytes, dest, quality)
        except Exception:
            raise pil_err


_tls = threading.local()


def worker_ftp(cfg: Config) -> ftplib.FTP:
    ftp = getattr(_tls, "ftp", None)
    if ftp is None:
        ftp = make_ftp(cfg)
        _tls.ftp = ftp
    return ftp


def _reset_worker_ftp() -> None:
    ftp: ftplib.FTP | None = getattr(_tls, "ftp", None)
    if isinstance(ftp, ftplib.FTP):
        try:
            ftp.close()
        except (ftplib.all_errors, OSError):
            pass
        _tls.ftp = None


def download_file(ftp: ftplib.FTP, path: str) -> bytes:
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {path}", buf.write)
    return buf.getvalue()


def download_and_convert(
    cfg: Config,
    rel_remote: PurePosixPath,
) -> tuple[PurePosixPath, bool, str]:
    remote_full = str(cfg.remote_root / rel_remote)
    src_suffix = Path(rel_remote.name).suffix
    image_bytes = b""
    for attempt in range(2):
        try:
            ftp = worker_ftp(cfg)
            image_bytes = download_file(ftp, remote_full)
            break
        except (ftplib.error_temp, ftplib.error_proto, EOFError, OSError) as e:
            _reset_worker_ftp()
            if attempt == 1:
                return rel_remote, False, f"전송 실패({type(e).__name__}): {e}"
    try:
        dest = cfg.local_root / src_to_jpg_relpath(rel_remote)
        convert_to_jpg(image_bytes, src_suffix, dest, cfg.jpg_quality)
        return rel_remote, True, ""
    except Exception as e:
        return (
            rel_remote,
            False,
            f"변환 실패({len(image_bytes)}B, {src_suffix}, {type(e).__name__}): {e}",
        )


def diff(
    cfg: Config,
    remote: dict[PurePosixPath, RemoteFile],
    state: State,
) -> tuple[list[PurePosixPath], list[str]]:
    to_pull: list[PurePosixPath] = []
    seen: set[str] = set()
    for rel, rf in remote.items():
        key = str(rel)
        seen.add(key)
        entry = state.entries.get(key)
        jpg_path = cfg.local_root / src_to_jpg_relpath(rel)
        if (
            entry is None
            or entry.get("size") != rf.size
            or int(entry.get("mtime", 0)) != rf.mtime
            or not jpg_path.exists()
        ):
            to_pull.append(rel)
    to_delete = sorted(k for k in state.entries if k not in seen)
    return to_pull, to_delete


def prune_empty_dirs(root: Path) -> None:
    for p in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda x: len(x.parts),
        reverse=True,
    ):
        try:
            p.rmdir()
        except OSError:
            pass


def _close_ftp_quietly(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except (ftplib.all_errors, OSError):
        ftp.close()


def _index_nas(cfg: Config) -> dict[PurePosixPath, RemoteFile]:
    LOG.info(
        "NAS 인덱싱: %s@%s:%s (FTP%s)",
        cfg.username,
        cfg.host,
        cfg.remote_root,
        "S" if cfg.use_tls else "",
    )
    ftp = make_ftp(cfg)
    try:
        return list_remote_images(ftp, cfg.remote_root)
    finally:
        _close_ftp_quietly(ftp)


def _record_success(
    rel: PurePosixPath,
    remote: dict[PurePosixPath, RemoteFile],
    state: State,
) -> None:
    rf = remote[rel]
    state.update(
        rel=str(rel),
        size=rf.size,
        mtime=rf.mtime,
        jpg=str(PurePosixPath(*src_to_jpg_relpath(rel).parts)),
    )


def _run_conversions(
    cfg: Config,
    to_pull: list[PurePosixPath],
    remote: dict[PurePosixPath, RemoteFile],
    state: State,
) -> list[tuple[PurePosixPath, str]]:
    failed: list[tuple[PurePosixPath, str]] = []
    last_save = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
            futures = {
                ex.submit(download_and_convert, cfg, rel): rel for rel in to_pull
            }
            with tqdm(total=len(futures), desc="변환", unit="장") as bar:
                for fut in as_completed(futures):
                    rel, ok, err = fut.result()
                    bar.update(1)
                    if ok:
                        _record_success(rel, remote, state)
                        if time.monotonic() - last_save > 10:
                            state.save()
                            last_save = time.monotonic()
                    else:
                        failed.append((rel, err))
                        LOG.error("실패 %s: %s", rel, err)
    finally:
        state.save()
    return failed


def _apply_deletions(cfg: Config, to_delete: list[str], state: State) -> None:
    for key in to_delete:
        entry = state.entries.get(key, {})
        jpg_rel = entry.get("jpg")
        if isinstance(jpg_rel, str) and jpg_rel:
            try:
                (cfg.local_root / jpg_rel).unlink(missing_ok=True)
                LOG.info("삭제: %s", jpg_rel)
            except OSError as e:
                LOG.error("삭제 실패 %s: %s", jpg_rel, e)
        state.remove(key)
    state.save()
    prune_empty_dirs(cfg.local_root)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NAS 이미지 → 로컬 JPG 단방향 동기화")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "config.toml")
    parser.add_argument("--delete", action="store_true", help="NAS에 없는 추적 항목의 로컬 JPG 삭제")
    parser.add_argument("--dry-run", action="store_true", help="실행하지 않고 계획만 표시")
    parser.add_argument("--force", action="store_true", help="상태 파일을 무시하고 전부 재변환")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _print_plan(to_pull: list[PurePosixPath], to_delete: list[str]) -> None:
    for r in to_pull:
        print(f"PULL  {r}")
    for d in to_delete:
        print(f"DEL   {d}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cfg = Config.load(args.config)
    cfg.local_root.mkdir(parents=True, exist_ok=True)
    state_path = cfg.local_root / STATE_FILENAME
    state = State(state_path, {}) if args.force else State.load(state_path)

    remote = resolve_collisions(_index_nas(cfg))
    LOG.info("원격 이미지 %d개 / 추적 중인 항목 %d개", len(remote), len(state.entries))

    to_pull, to_delete = diff(cfg, remote, state)
    LOG.info("변환 대상 %d개 / 삭제 후보 %d개", len(to_pull), len(to_delete))

    if args.dry_run:
        _print_plan(to_pull, to_delete)
        return 0

    failed = _run_conversions(cfg, to_pull, remote, state) if to_pull else []

    if args.delete and to_delete:
        _apply_deletions(cfg, to_delete, state)

    if failed:
        LOG.warning("실패 %d건 (다음 실행에서 자동 재시도)", len(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
