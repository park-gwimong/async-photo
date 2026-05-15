# AsyncPhoto

NAS에 보관된 RAW/이미지 파일을 FTP로 받아 로컬에 통일된 JPG 포맷으로 단방향 동기화하는 CLI 도구.

## 특징

- **재실행 안전** — 매니페스트 파일로 처리 상태를 추적해 같은 작업을 중복 수행하지 않음
- **폴더 구조 유지** — NAS의 디렉터리 트리를 그대로 로컬에 미러링
- **다양한 포맷 지원** — Nikon NEF를 비롯한 주요 RAW와 일반 이미지(JPG/PNG/TIFF/HEIC/...)를 동일한 JPG 설정으로 통일
- **충돌 자동 해결** — RAW + 동행 JPG처럼 같은 이름의 원본이 여러 개면 자동으로 하나만 선택
- **중단 안전** — 중간 저장으로 강제 종료되어도 다음 실행에서 이어감

## 설치

```bash
# 1) 가상환경
python -m venv .venv
source .venv/bin/activate

# 2) 의존성
pip install -r requirements.txt

# 3) (옵션) HEIC/HEIF 지원
brew install libheif          # macOS
pip install pillow-heif
```

`rawpy`는 LibRaw 바이너리가 포함된 휠로 설치되므로 추가 시스템 패키지가 필요 없습니다.

## 설정

`config.example.toml`을 복사해 `config.toml`을 만들고 본인 환경을 채워 넣습니다.

```bash
cp config.example.toml config.toml
chmod 600 config.toml
```

### 설정 항목

| 섹션 | 키 | 설명 |
|---|---|---|
| `[ftp]` | `host` | NAS 호스트명 또는 IP |
| | `port` | FTP 포트 (기본 21) |
| | `username` / `password` | FTP 계정 |
| | `use_tls` | true면 FTPS(명시적 TLS)로 접속 |
| `[sync]` | `remote_root` | NAS의 동기화 대상 최상위 경로 |
| | `local_root` | 로컬 저장 위치 (`~` 사용 가능) |
| | `jpg_quality` | JPEG 품질(0–100, 기본 88) |
| | `workers` | 동시 다운로드 워커 수 (기본 3) |

## 사용

```bash
python sync.py              # 평상시 실행 (다운로드 + 변환)
python sync.py --dry-run    # 무엇이 변환/삭제될지 계획만 출력
python sync.py --delete     # NAS에서 사라진 항목의 로컬 JPG 삭제
python sync.py --force      # 매니페스트 무시하고 전부 재변환
python sync.py -v           # 디버그 로그
```

옵션은 조합 가능합니다. 예) `python sync.py --delete --dry-run`.

## 지원 포맷

| 분류 | 디코더 | 확장자 |
|---|---|---|
| RAW | rawpy(LibRaw) | `.nef .nrw .arw .srf .sr2 .cr2 .cr3 .crw .dng .orf .rw2 .raf .pef .srw .x3f .3fr .kdc .dcr .mrw .rwl` |
| 일반 이미지 | Pillow | `.jpg .jpeg .jpe .png .tif .tiff .bmp .webp .gif` |
| HEIC | Pillow + pillow-heif | `.heic .heif` (옵션) |

모든 결과물은 `quality=88, optimize, progressive, 4:2:0 subsampling`의 통일된 JPEG 설정으로 저장됩니다.

## 동작 방식

### 변경 감지

각 NAS 파일의 `(상대경로, size, mtime)`을 `local_root/.syncphoto-state.json`에 기록합니다. 다음 실행에서 size 또는 mtime이 바뀐 항목만 다운로드/변환합니다. 로컬 JPG가 사라진 항목은 자동으로 재변환됩니다.

### 이름 충돌

원본 여러 개가 같은 출력 경로(`with_suffix(".jpg")` 기준)로 매핑되면 다음 우선순위로 하나만 선택됩니다:

1. JPG/JPEG (재인코딩만 하므로 가장 빠름)
2. 그 외 일반 이미지(PNG/TIFF/...)
3. RAW (디코딩 비용이 가장 큼)

예) `IMG_001.NEF` + `IMG_001.JPG` 페어가 있으면 JPG가 선택되고 NEF는 워닝과 함께 제외됩니다.

### 제외되는 파일

- `._*` (macOS AppleDouble 사이드카)
- `.DS_Store`, `Thumbs.db`
- 0바이트 파일

### 중간 저장

대량 처리 중에는 10초 간격으로 매니페스트를 디스크에 저장합니다. `Ctrl+C` 등으로 중단해도 완료된 항목은 다음 실행에서 스킵됩니다.

## 보안 노트

- `config.toml`에는 **평문 비밀번호**가 들어갑니다. `chmod 600 config.toml`로 권한을 제한하고 git 저장소라면 `.gitignore`에 추가하세요.
- 외부 네트워크 너머의 NAS라면 `use_tls = true`로 FTPS를 활성화하세요. LAN 내부라면 큰 위협은 아닙니다.
- 본 도구는 NAS → 로컬 **단방향**입니다. 로컬에서 변경된 내용은 NAS로 반영되지 않습니다.

## 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| `UnidentifiedImageError` | 손상 파일이거나 확장자가 잘못된 경우. 도구가 자동으로 RAW로 재시도하지만 실패 시 해당 파일만 스킵하고 다음 실행에서 재시도됩니다. |
| `HEIC/HEIF 변환을 위해 pillow-heif를 설치하세요` | `pip install pillow-heif` (macOS는 `brew install libheif` 선행) |
| MLSD 미지원 (`error_perm 500`) | 구형 FTP 서버. NAS를 최신 펌웨어로 업데이트하거나 FTPS로 전환을 시도해 보세요. |
| 동시 다운로드가 너무 느림/빠름 | `config.toml`의 `workers` 값 조정 |

## 파일 구성

```
AsyncPhoto/
  sync.py                       # 메인 스크립트
  config.example.toml           # 설정 샘플
  config.toml                   # 실제 설정 (직접 생성, gitignore 권장)
  requirements.txt              # 의존성
  README.md                     # 이 파일
  <local_root>/.syncphoto-state.json   # 매니페스트 (자동 생성)
```
