# lekiwi_net

LeKiwi 로봇(Raspberry Pi, Ubuntu 24.04)의 **와이파이를 바꾸는 스크립트 모음**이다.
로봇은 두 가지 방식으로 네트워크에 붙는다. 이 폴더의 스크립트가 둘 사이를 오가고, 각 모드의 설정을 고친다.

- **AP 모드** — 로봇이 직접 와이파이를 띄운다. PC를 그 와이파이에 붙여서 `ssh <사용자>@192.168.4.1`로 들어간다.
- **공유기 모드** — 로봇이 집·사무실 공유기에 붙는다. 공유기가 안 보이면 자동으로 AP가 뜬다.

NetworkManager(`nmcli`)를 쓰는 카드용이다. 로봇의 `~/lekiwi_net`에 두고 쓴다 (**이 경로 그대로** — 앱과 `.bashrc`가 이 경로를 부른다).

**목차**

1. [파일 한눈에](#1-파일-한눈에)
2. [짧은 명령 (.bashrc)](#2-짧은-명령-bashrc)
3. [자주 하는 일](#3-자주-하는-일)
4. [새 카드 복제 후 초기화](#4-새-카드-복제-후-초기화)
5. [어떻게 안전하게 바꾸나](#5-어떻게-안전하게-바꾸나)
6. [앱이 읽는 출력 줄](#6-앱이-읽는-출력-줄)
7. [환경 변수](#7-환경-변수)
8. [알아둘 점](#8-알아둘-점)
9. [관련 저장소](#9-관련-저장소)

---

## 1. 파일 한눈에

| 파일 | 하는 일 |
|---|---|
| `run_ap.sh` | wlan0를 **AP 모드**로. 재부팅해도 유지. 로봇 IP `192.168.4.1` |
| `run_wifi.sh` | wlan0를 **공유기 모드**로. 인자 없이 = 저장된 공유기로 연결, `SSID PASSWORD` = 새 공유기 저장(DHCP) 후 연결. 연결 실패 시 AP로 복귀 |
| `set_ap.sh` | AP 이름 · 비밀번호 변경. 인자 없이 = 현재 AP 이름 보기 |
| `set_ip.sh` | 공유기 모드의 **고정 IP** 설정: `192.168.0.(200 + 킷 번호)`. 인자 없이 = 현재 설정 보기 |
| `set_dhcp.sh` | 공유기 모드를 **DHCP**로 되돌린다 |
| `_apply_home_ipv4.sh` | `set_ip.sh` · `set_dhcp.sh`가 같이 쓰는 내부 스크립트. 직접 부르지 않는다 |
| `wifi_list.sh` | 주변 와이파이 목록 (SSID · 신호 · 대역 · 보안). `iw`로 스캔하므로 NetworkManager와 무관 |
| `net_status.sh` | 지금 켜진 연결과 IPv4 주소 보기 |
| `clone_init.sh` | 복제한 카드마다 **한 번**: 호스트 이름 · 킷 번호 · AP 채널 · 공유기 고정 IP 설정 |
| `nm_kf.py` | NetworkManager 설정 파일(keyfile) 편집기. 위 스크립트가 `nmcli con modify` 대신 쓴다 |

---

## 2. 짧은 명령 (.bashrc)

이미지에는 `.bashrc`에 짧은 이름이 들어 있다 (정의는 `lekiwi-gui-ap_mode/web_operator/robot_net/bashrc_net.sh`).
이 문서와 스크립트 주석의 명령 이름은 아래 표를 따른다.

| 짧은 명령 | 실제로 부르는 것 |
|---|---|
| `run_ap` | `~/lekiwi_net/run_ap.sh` |
| `run_wifi [SSID PASSWORD]` / `wifi [SSID PASSWORD]` | `~/lekiwi_net/run_wifi.sh` |
| `wifi list [인터페이스]` | `~/lekiwi_net/wifi_list.sh` |
| `set_ap [SSID PASSWORD]` | `~/lekiwi_net/set_ap.sh` |
| `setip [킷번호]` | `~/lekiwi_net/set_ip.sh` |
| `setdhcp` / `set_dhcp` | `~/lekiwi_net/set_dhcp.sh` |
| `net_status` | `~/lekiwi_net/net_status.sh` |

### `.bashrc`에 넣는 방법

로봇의 `~/.bashrc`에 짧은 명령이 아직 없을 때만 한다 (이미지에는 이미 들어 있다). 먼저 확인:

```bash
grep -c 'lekiwi_net >>>' ~/.bashrc   # 1 이상이면 이미 있다 — 아래를 또 붙이지 않는다
```

없으면 파일을 열어서 **맨 아래에** 아래 블록을 통째로 붙여넣는다.

```bash
cd ~
nano ~/.bashrc
```

```bash
# >>> lekiwi_net >>>
# 와이파이 전환 단축 명령: wifi <SSID> <비밀번호>  (= run_wifi, 저장된 공유기면 인자 없이 wifi)
#   wifi list [인터페이스]  → 주변 와이파이 목록
wifi() {
	if [ "${1:-}" = "list" ]; then
		"$HOME/lekiwi_net/wifi_list.sh" "${2:-}"
	else
		"$HOME/lekiwi_net/run_wifi.sh" "$@"
	fi
}

# 킷 고정 IP 변경 단축 명령: setip <킷번호>
setip() {
	"$HOME/lekiwi_net/set_ip.sh" "$@"
}

# wlan0 DHCP 전환 단축 명령: setdhcp / set_dhcp (고정 IP 복귀는 setip N)
setdhcp() {
	"$HOME/lekiwi_net/set_dhcp.sh" "$@"
}
alias set_dhcp=setdhcp

# LeKiwi 네트워크 모드 전환 (~/lekiwi_net)
alias run_ap="~/lekiwi_net/run_ap.sh"
alias run_wifi="~/lekiwi_net/run_wifi.sh"
alias set_ap="~/lekiwi_net/set_ap.sh"
alias net_status="~/lekiwi_net/net_status.sh"
# <<< lekiwi_net <<<
```

저장하고 나온 뒤(`Ctrl+O` → `Enter` → `Ctrl+X`) 지금 터미널에 적용한다.

```bash
source ~/.bashrc
```

`# >>> lekiwi_net >>>` · `# <<< lekiwi_net <<<` 줄은 지우지 않는다. 설치 스크립트(`provision_ap_mode.sh`)가 `# >>> lekiwi_net >>>` 줄로 "이미 들어 있나"를 확인하고, 있으면 다시 붙이지 않는다.
정의의 원본은 `lekiwi-gui-ap_mode/web_operator/robot_net/bashrc_net.sh`이므로, 고칠 때는 그쪽과 이 블록을 같이 맞춘다.

`.bashrc`에 넣지 않았다면 `~/lekiwi_net/run_wifi.sh ...`처럼 경로로 부른다.

---

## 3. 자주 하는 일

### 지금 어떻게 붙어 있나

```bash
net_status
```

켜진 연결 이름 · 장치 · 상태와 IPv4 주소를 보여준다.

### AP 모드로 (로봇에 직접 붙기)

```bash
run_ap
# [run_ap] AP 모드 ON -> PC 를 '<AP 이름>' 에 연결 후: ssh <사용자>@192.168.4.1
```

공유기 프로필의 자동 연결을 꺼서, 재부팅해도 AP로 뜬다.

### 공유기 모드로

```bash
wifi list                      # 주변 와이파이 확인
run_wifi MyRouter MyPassword   # 처음 한 번: 저장 + 연결 (DHCP)
run_wifi                       # 이후: 저장된 공유기로 연결
```

- 비밀번호는 8~63자 (WPA2). SSID · 비밀번호 앞뒤에 공백을 둘 수 없다.
- 새 공유기를 저장하면 **고정 IP 설정은 DHCP로 초기화**된다. 고정 IP가 필요하면 이어서 `setip N`.
- 연결에 실패하면 자동으로 AP 모드로 돌아와서, 로봇에 닿을 길이 끊기지 않는다.

### 공유기 모드의 IP 바꾸기

```bash
setip          # 현재 설정 보기
setip 3        # 192.168.0.203 으로 고정 (킷 번호 1~54)
setdhcp        # 공유기가 주는 주소를 받는다
```

적용 방식은 [5절](#5-어떻게-안전하게-바꾸나) 참고. AP 모드일 때는 **저장만** 하고, 다음 `run_wifi` 때 적용된다.

### AP 이름 · 비밀번호 바꾸기

```bash
set_ap                       # 현재 AP 이름 보기
set_ap MyLeKiwi MyPassword   # 변경 (이름 1~32바이트, 비밀번호 8~63자)
```

AP가 켜져 있으면 바로 다시 올라오므로 PC를 새 이름으로 다시 연결해야 한다.

---

## 4. 새 카드 복제 후 초기화

이미지를 구운 카드마다 **한 번** 실행한다. 호스트 이름은 `lekiwi%02d(킷 번호)`여야 한다.

```bash
sudo ~/lekiwi_net/clone_init.sh lekiwi07 7
sudo ~/lekiwi_net/clone_init.sh lekiwi07 7 --ap-ssid NAME --ap-password PW   # AP 이름 · 비밀번호도 정할 때
sudo reboot
```

| 항목 | 규칙 |
|---|---|
| 호스트 이름 | `lekiwi07` — 킷 번호와 맞지 않으면 거절 |
| 킷 번호 | 1~54. `/etc/kit-number`에도 저장 |
| AP 채널 | `(1, 6, 11)[킷 번호 % 3]` — 옆 카드와 채널이 겹치지 않게 |
| AP 이름 · 비밀번호 | 옵션을 줄 때만 바꾼다. 처음 값은 모든 로봇이 같고 고객이 바꾼다 |
| 공유기 고정 IP | `home-wifi` 프로필이 있을 때만 `192.168.0.(200 + 킷 번호)` |

종료 코드: `0` 성공 / `1` 인자 오류 또는 실패한 단계.
SSH 호스트키 · machine-id는 이미지의 첫 부팅 서비스가 새로 만든다 (여기서는 하지 않는다).

---

## 5. 어떻게 안전하게 바꾸나

**`nmcli con modify`를 쓰지 않는다.** Ubuntu 24.04의 NetworkManager에 netplan 저장 버그가 있어,
`nm_kf.py`로 keyfile(`/etc/NetworkManager/system-connections/*.nmconnection`)을 직접 고치고 `nmcli con load`로 읽힌다.

```bash
nm_kf.py get NAME SECTION KEY
nm_kf.py set NAME SECTION KEY VALUE [SECTION KEY VALUE ...]
nm_kf.py del NAME SECTION KEY [KEY ...]
```

파일은 임시 파일에 쓴 뒤 권한 `600`으로 교체한다 (쓰다 끊겨도 반쪽 파일이 남지 않는다).

**원격으로 바꾸다 접속이 끊기지 않도록**, 실패하면 이전 상태로 되돌리는 단계를 둔다.

| 스크립트 | 실패했을 때 |
|---|---|
| `run_wifi.sh` | 공유기 연결 실패 → AP 모드로 복귀 |
| `_apply_home_ipv4.sh` (`setip` · `setdhcp`) | 적용한 IP가 안 잡힘 → 이전 설정 복구 → 복구도 실패하면 AP로 전환 |
| `run_ap.sh` | AP 전환 실패 → 공유기 자동 연결 설정을 원래 값으로 되돌리고, 공유기 우선 모드였으면 공유기로 복귀 |

**비밀번호가 흔적으로 남지 않게 한다.** `_apply_home_ipv4.sh`는 이전 설정 사본(공유기 비밀번호 포함)을 쓴 뒤 항상 지운다.
`clone_init.sh`는 AP 비밀번호가 `sudo` 명령줄(`auth.log`)에 남지 않도록 `nm_kf.py`를 직접 부른다.

---

## 6. 앱이 읽는 출력 줄

Physical Labs 앱이 `run_wifi.sh` · `set_ip.sh` · `set_dhcp.sh`의 출력을 읽어 결과를 판단한다. **아래 줄의 모양을 바꾸면 앱이 깨진다.**

| 줄 | 의미 |
|---|---|
| `inet <IP>/<prefix> ...` (`ip -4 -o addr show wlan0` 출력) | 연결 성공. 앱이 IP를 여기서 읽는다 |
| `error: ...` | 실패. 앱이 읽는 줄 |

`wifi_list.sh`는 보안 표기(WPA2/3 · WPA · WEP · 개방)를 게이트웨이(`network.py`의 `parse_iw_scan`)와 같게 쓰고, 신호 임계값(-55 / -67 / -78 dBm)은 앱이 이 스크립트와 같게 쓴다.
숨김 SSID 판정만 게이트웨이와 조금 다르다 (여기서는 `\x00`이 하나라도 있으면 숨김). 바꿀 때는 그쪽도 같이 본다.

---

## 7. 환경 변수

시험용이다. 평소에는 설정하지 않는다.

| 변수 | 기본값 | 용도 |
|---|---|---|
| `NM_KF_DIR` | `/etc/NetworkManager/system-connections` | keyfile 폴더 |
| `LEKIWI_NET_STATE_DIR` | `/var/lib/lekiwi-net` | 이전 설정 사본을 잠깐 두는 폴더 |
| `CLONE_ROOT` | (없음) | `clone_init.sh`의 모든 절대 경로 앞에 붙는다. 가짜 루트로 시험할 때만 쓰고, **root로는 실행하지 않는다** |

---

## 8. 알아둘 점

- **공유기 대역은 `192.168.0.0/24`, 게이트웨이는 `192.168.0.1`로 가정**한다 (`_apply_home_ipv4.sh`에 고정). 다른 대역 공유기에서는 `setip`이 맞지 않는다. 그럴 땐 `setdhcp`를 쓴다.
- 고정 IP는 킷 번호 1~54만 된다 (`.201`~`.254`).
- DHCP로 쓸 때 DNS는 공유기가 주는 값을 따르고, 고정 IP일 때만 `8.8.8.8` · `1.1.1.1`을 쓴다.
- 이미지 직후 카드에는 `home-wifi` 프로필이 없다. 이때 `run_wifi`(인자 없이) · `setip N` · `setdhcp`는 "먼저 `run_wifi SSID PW`"라고 안내하고 끝난다.
- AP가 비정상 종료되어 `192.168.4.1`을 `dnsmasq`가 붙잡고 있으면 `run_ap`가 정리하고 다시 올린다.
- 대부분 `sudo`가 필요하다 (스크립트 안에서 부르므로 따로 `sudo`를 붙일 필요는 없다. `clone_init.sh`만 `sudo`로 실행).

---

## 9. 관련 저장소

- `lekiwi-gui-ap_mode` — `web_operator/robot_net/bashrc_net.sh`(짧은 명령 정의)와 `image_ready_check.sh`(이미지를 뜨기 전 점검)가 이 폴더를 `~/lekiwi_net` 경로로 부른다
- `lekiwi_ws` — 로봇의 ROS 2 워크스페이스
