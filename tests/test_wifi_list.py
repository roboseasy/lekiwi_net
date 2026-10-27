"""wifi_list.sh 시험: `wifi list`가 부르는 주변 와이파이 목록 (iw 스캔 출력을 읽기만 한다).

가짜 PATH(test_robot_net의 FakeBin)에서 `iw`는 고정된 스캔 출력을 돌려주는 stub이다 (`sudo`는 기본 stub).
스크립트를 직접 실행해서 shebang · 실행 권한도 함께 본다.
"""

import os
import subprocess
from pathlib import Path

import pytest

from test_robot_net import ROBOT_NET, FakeBin

WIFI_LIST = ROBOT_NET / 'wifi_list.sh'
BASHRC_NET = ROBOT_NET / 'bashrc_net.sh'

IW_STUB = '''echo "iw $*" >> "$FAKE_LOG"
case "$*" in
dev) [ "${FAKE_IW_NO_IFACE:-0}" = 1 ] || printf 'phy#0\\n\\tInterface wlan0\\n\\t\\tifindex 3\\n' ;;
'dev wlan0 scan') [ "${FAKE_IW_NO_SCAN:-0}" = 1 ] || cat "$FAKE_IW_SCAN" ;;
esac
'''


def bss(mac: str, freq: int, signal: int, ssid: str, flags: str, *ies: str) -> str:
	"""`iw dev wlan0 scan`의 BSS 한 덩어리. 숨김 SSID는 실제 iw처럼 `SSID: `로 끝에 공백이 남는다."""
	return (f'BSS {mac}(on wlan0)\n\tfreq: {freq}.0\n\tcapability: {flags}\n\tsignal: {signal}.00 dBm\n\tSSID: {ssid}\n'
		+ ''.join(f'\t{ie}:\t * Version: 1\n' for ie in ies))


# 신호는 막대 임계값(-55 · -67 · -78, Physical Labs 앱도 같은 값)의 경계 양쪽에 둔다.
# 같은 SSID는 약한 쪽이 먼저 나온다 ('처음 본 것'을 남기는 회귀를 잡는다).
SCAN = ''.join([
	bss('88:36:6c:12:34:57', 2437, -71, 'Office', 'ESS Privacy (0x0011)', 'RSN'),
	bss('88:36:6c:12:34:56', 5180, -56, 'Office', 'ESS Privacy (0x0011)', 'RSN'),
	bss('00:11:22:33:44:55', 2412, -67, 'Old', 'ESS Privacy (0x0011)', 'WPA'),
	bss('00:11:22:33:44:aa', 2412, -60, 'Mixed', 'ESS Privacy (0x0011)', 'WPA', 'RSN'),     # WPA/WPA2 혼합: RSN이 이긴다
	bss('00:11:22:33:44:66', 2462, -78, 'Legacy', 'ESS Privacy (0x0011)'),
	bss('00:11:22:33:44:77', 2412, -55, 'Cafe', 'ESS (0x0001)'),
	bss('00:11:22:33:44:88', 5745, -68, '', 'ESS Privacy (0x0011)', 'RSN'),
	bss('00:11:22:33:44:99', 5825, -79, 'Far', 'ESS Privacy (0x0011)', 'RSN'),
	bss('00:11:22:33:44:bb', 2437, -90, '\\x00' * 4, 'ESS Privacy (0x0011)', 'RSN'),      # iw가 찍는 NUL 이름: 숨김 줄에 합쳐진다
])


def run(tmp_path: Path, *args: str, **env: str) -> tuple[subprocess.CompletedProcess, FakeBin]:
	scan = tmp_path / 'scan.txt'
	scan.write_text(SCAN)
	fake = FakeBin(tmp_path, {'iw': IW_STUB})
	return fake.run([str(WIFI_LIST), *args], FAKE_IW_SCAN=str(scan), **env), fake


# `wifi list`는 .bashrc에서 인터페이스 자리에 빈 인자를 넘긴다 (bashrc_net.sh의 "${2:-}")
@pytest.mark.parametrize('args', [(), ('',), ('wlan0',)], ids=['no-arg', 'empty-arg', 'wlan0'])
def test_lists_strongest_per_ssid_first_with_band_security_and_bars(tmp_path: Path, args: tuple[str, ...]) -> None:
	done, fake = run(tmp_path, *args)
	assert done.returncode == 0, done.stderr
	rows = [line.split() for line in done.stdout.splitlines() if ' dBm ' in line]
	assert [(row[0], row[1], row[4], row[5], len(row[3])) for row in rows] == [
		('Cafe', '-55', '2.4GHz', '개방', 4),
		('Office', '-56', '5GHz', 'WPA2/3', 3),
		('Mixed', '-60', '2.4GHz', 'WPA2/3', 3),
		('Old', '-67', '2.4GHz', 'WPA', 3),
		('(숨김)', '-68', '5GHz', 'WPA2/3', 2),
		('Legacy', '-78', '2.4GHz', 'WEP', 2),
		('Far', '-79', '5GHz', 'WPA2/3', 1),
	]
	assert 'wlan0 (7개)' in done.stdout
	if os.geteuid() != 0:                                  # root가 아니면 스캔은 sudo로 부른다
		assert 'sudo iw dev wlan0 scan' in fake.calls()


@pytest.mark.parametrize('env, message', [
	({'FAKE_IW_NO_IFACE': '1'}, '무선 인터페이스를 찾지 못했습니다'),
	({'FAKE_IW_NO_SCAN': '1'}, '스캔에 실패했습니다'),
])
def test_reports_missing_interface_and_failed_scan(tmp_path: Path, env: dict[str, str], message: str) -> None:
	done, _ = run(tmp_path, **env)
	assert done.returncode == 1
	assert message in done.stderr


def test_wifi_command_from_the_bashrc_block_runs_the_installed_script(tmp_path: Path) -> None:
	"""bashrc_net.sh의 `wifi list`가 ~/lekiwi_net/wifi_list.sh를 부른다 (옮긴 뒤 경로가 어긋나면 'No such file')."""
	scan = tmp_path / 'scan.txt'
	scan.write_text(SCAN)
	fake = FakeBin(tmp_path, {'iw': IW_STUB})
	installed = Path(fake.env['HOME']) / 'lekiwi_net' / 'wifi_list.sh'
	installed.parent.mkdir()
	installed.write_bytes(WIFI_LIST.read_bytes())
	installed.chmod(0o755)
	done = fake.run(['bash', '-c', f'source {BASHRC_NET}; wifi list'], FAKE_IW_SCAN=str(scan))
	assert done.returncode == 0, done.stderr
	assert 'wlan0 (7개)' in done.stdout
