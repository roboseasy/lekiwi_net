"""provision_ap_mode.sh · rollback_ap_mode.sh 시험.

임시 PROVISION_ROOT에 로봇 모양 트리(netplan 세대, 가짜 비밀번호)를 만들고, 가짜 PATH(test_robot_net의
FakeBin)에서만 돌린다. apt-get · systemctl · netplan · iw · systemd-run도 stub이라 이 PC는 바뀌지 않는다.
"""

import configparser
import getpass
import os
import re
import signal
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from test_robot_net import ROBOT_NET, FakeBin, index_of, tree_state

PROVISION = ROBOT_NET / 'provision_ap_mode.sh'
ROLLBACK = ROBOT_NET / 'rollback_ap_mode.sh'
ROUTER_PSK = 'FAKEPSK-12345'
HOME = '/home/roboseasy'

NETPLAN = '''network:
  version: 2
  ethernets:
    eth0:
      optional: true
      dhcp4: true
  wifis:
    wlan0:
      optional: true
      dhcp4: true
      access-points:
        "roboseasy":
          password: "FAKEPSK-12345"
'''

NETPLAN_WITHOUT_WIFIS = '''network:
  version: 2
  ethernets:
    eth0:
      optional: true
      dhcp4: true
'''

NETWORK_CONFIG = '''# cloud-init network config (first boot)
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
      optional: true
  wifis:
    wlan0:
      dhcp4: true
      optional: true
      access-points:
        "roboseasy":
          password: "FAKEPSK-12345"
'''

OLD_BASHRC = '''# ~/.bashrc
wifi() {
	"$HOME/set_wifi.sh" "$@"
}
setip() { "$HOME/set_ip.sh" "$@"; }
setdhcp() { "$HOME/set_dhcp.sh" "$@"; }
'''

EXTRA_STUBS: dict[str, str] = {
	# 설치 순간에 wlan0을 붙잡는 파일이 이미 있었는지 그 내용과 함께 남긴다.
	# 설치 순간에 wlan0을 붙잡는 파일이 있었는지 그 내용과, umask · needrestart 설정을 함께 남긴다.
	# FAKE_APT_FAILS=1: 설치 실패.
	'apt-get': '''hold="$PROVISION_ROOT/etc/NetworkManager/conf.d/90-lekiwi-hold.conf"
if [ -f "$hold" ]; then h=$(tr '\\n' ' ' < "$hold"); else h=none; fi
echo "apt-get $* | hold: $h | umask $(umask) NEEDRESTART_SUSPEND=${NEEDRESTART_SUSPEND:-}" >> "$FAKE_LOG"
[ "${FAKE_APT_FAILS:-0}" != 1 ] || { echo 'E: fake: install failed' >&2; exit 100; }
''',
	'apt-mark': 'echo "apt-mark $*" >> "$FAKE_LOG"\n',
	# 유닛 상태는 "$FAKE_LOG.active.<유닛>" 파일이 있으면 active (stop이 지운다).
	# FAKE_ACTIVE_UNITS: 무엇을 해도 active로 남는 유닛 (공백으로 여럿).
	'systemctl': '''echo "systemctl $*" >> "$FAKE_LOG"
unit=${!#}
case "${1:-}" in
is-active)
	for u in ${FAKE_ACTIVE_UNITS:-}; do [ "$u" = "$unit" ] && { echo active; exit 0; }; done
	[ -e "$FAKE_LOG.active.$unit" ] && { echo active; exit 0; }
	echo inactive; exit 3 ;;
stop) rm -f "$FAKE_LOG.active.$unit" ;;
show) if [ -e "$FAKE_LOG.active.$unit" ]; then echo waiting; else echo dead; fi ;;     # show -p SubState --value 타이머
esac
exit 0
''',
	# FAKE_NETPLAN_FAIL_ON: 실패할 호출 번호 (공백으로 여럿). FAKE_NETPLAN_BLOCK=generate: 그 하위 명령에서 멈춘다.
	# apply는 netplan에 wifis가 있으면 netplan-wpa-wlan0을 켜고(FAKE_WPA_STAYS_DOWN=1이면 못 켬) 없으면 끈다.
	'netplan': '''echo "netplan $*" >> "$FAKE_LOG"
n=$(( $(cat "$FAKE_LOG.netplan" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_LOG.netplan"
case " ${FAKE_NETPLAN_FAIL_ON:-} " in *" $n "*) echo 'fake: netplan failed' >&2; exit 1 ;; esac
[ "${1:-}" != "${FAKE_NETPLAN_BLOCK:-}" ] || sleep 30
if [ "${1:-}" = apply ]; then
	if grep -qs '^ *wifis:' "$PROVISION_ROOT"/etc/netplan/*.yaml && [ "${FAKE_WPA_STAYS_DOWN:-0}" != 1 ]; then
		: > "$FAKE_LOG.active.netplan-wpa-wlan0.service"
	else rm -f "$FAKE_LOG.active.netplan-wpa-wlan0.service"; fi
fi
''',
	'iw': 'echo "iw $*" >> "$FAKE_LOG"\n',
	'pkill': 'echo "pkill $*" >> "$FAKE_LOG"\nexit 1\n',
	# 타이머를 켠다 ("$FAKE_LOG.active.<이름>.timer"). FAKE_TIMER_GONE=1: 타이머가 벌써 끝났다.
	'systemd-run': '''echo "systemd-run $*" >> "$FAKE_LOG"
for a in "$@"; do case $a in --unit=*) name=${a#--unit=} ;; esac; done
[ "${FAKE_TIMER_GONE:-0}" = 1 ] || : > "$FAKE_LOG.active.$name.timer"
''',
}


class RobotTree:
	"""PROVISION_ROOT 아래의 로봇 모양 트리와 그 위에서 스크립트를 돌리는 도구."""

	def __init__(self, base: Path, extra_stubs: dict[str, str] | None = None) -> None:
		self.fake = FakeBin(base, {**EXTRA_STUBS, **(extra_stubs or {})})
		self.root = base / 'root'
		self.home = self.root / HOME.lstrip('/')
		files = {
			'etc/netplan/50-cloud-init.yaml': (NETPLAN, 0o600),
			'etc/netplan/.50-cloud-init.yaml.bak': (NETPLAN, 0o600),
			'boot/firmware/network-config': (NETWORK_CONFIG, 0o644),
			'etc/networkd-dispatcher/routable.d/50-kit-ip': ('#!/usr/bin/env bash\nexit 0\n', 0o755),
			'etc/systemd/system/wifi-powersave-off.service': ('[Service]\nType=oneshot\n', 0o644),
			'etc/kit-number': ('4\n', 0o644),
			'etc/hostname': ('lekiwi04\n', 0o644),
			'etc/modprobe.d/blacklist.conf': ('blacklist x\n', 0o644),
			f'{HOME[1:]}/set_wifi.sh': ('#!/bin/bash\necho old\n', 0o755),
			f'{HOME[1:]}/set_ip.sh': ('#!/bin/bash\necho old\n', 0o755),
			f'{HOME[1:]}/set_dhcp.sh': ('#!/bin/bash\necho old\n', 0o755),
			f'{HOME[1:]}/wifi_list.sh': ('#!/bin/bash\niw dev wlan0 scan\n', 0o755),
			f'{HOME[1:]}/clone_init.sh': ('#!/bin/bash\necho old\n', 0o755),
			f'{HOME[1:]}/.bashrc': (OLD_BASHRC, 0o644),
		}
		for rel, (text, mode) in files.items():
			path = self.root / rel
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_text(text)
			path.chmod(mode)
		wants = self.root / 'etc/systemd/system/multi-user.target.wants'
		wants.mkdir()
		(wants / 'wifi-powersave-off.service').symlink_to('/etc/systemd/system/wifi-powersave-off.service')
		for folder in ('var/backups', 'var/lib', 'run'):
			(self.root / folder).mkdir(parents=True)
		Path(f'{self.fake.log}.active.netplan-wpa-wlan0.service').touch()     # netplan 세대 로봇은 이것이 wlan0을 잡고 있다

	def env(self, **extra: str) -> dict[str, str]:
		# wlan0은 설치 직후(붙잡은 동안) unmanaged, NetworkManager를 다시 켠 뒤로는 disconnected
		return {'PROVISION_ROOT': str(self.root), 'PROVISION_USER': getpass.getuser(), 'PROVISION_HOME': HOME,
			'FAKE_ACTIVE': 'home-wifi', 'FAKE_WLAN0_STATES': 'unmanaged disconnected', **extra}

	def provision(self, *args: str, **env: str) -> subprocess.CompletedProcess:
		return self.fake.run([str(PROVISION), *args], **self.env(**env))

	def rollback(self, *args: str, **env: str) -> subprocess.CompletedProcess:
		return self.fake.run([str(ROLLBACK), *args], **self.env(**env))

	def reset_device_states(self) -> None:
		"""nmcli stub의 wlan0 상태 차례를 처음부터 (한 시험에서 프로비저닝을 두 번 돌릴 때)."""
		Path(f'{self.fake.log}.dev').unlink(missing_ok=True)

	def text(self, rel: str) -> str:
		return (self.root / rel).read_text()

	def keyfile(self, name: str) -> configparser.RawConfigParser:
		parser = configparser.RawConfigParser(delimiters=('=',), interpolation=None)
		parser.optionxform = str
		parser.read_string(self.text(f'etc/NetworkManager/system-connections/{name}.nmconnection'))
		return parser

	def backups(self) -> list[Path]:
		return sorted((self.root / 'var/backups').glob('lekiwi_net_*.tar.gz'))

	def calls(self) -> list[str]:
		return self.fake.calls()


@pytest.fixture
def tree(tmp_path: Path) -> RobotTree:
	return RobotTree(tmp_path)


def ok(done: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
	assert done.returncode == 0, done.stdout + done.stderr
	return done


MUTATING = ('apt-get', 'apt-mark', 'systemctl', 'netplan', 'iw', 'systemd-run', 'nmcli con', 'sudo')


@pytest.mark.parametrize('env', [{'FAKE_ETH0_ADDR': ''}, {'FAKE_ETH0_CARRIER': '0'}])
def test_provision_refuses_without_wired_link(tree: RobotTree, env: dict[str, str]) -> None:
	before = tree_state(tree.root)
	done = tree.provision('--kit', '4', **env)
	assert done.returncode == 3, done.stdout + done.stderr
	assert 'eth0' in done.stderr
	assert tree_state(tree.root) == before
	assert not [line for line in tree.calls() if line.startswith(MUTATING)]


def test_provision_refuses_inside_lekiwi_net(tree: RobotTree) -> None:
	"""저장소를 ~/lekiwi_net 에 받아 그 안에서 돌리면 아무것도 바꾸기 전에 거절 (스크립트를 그 폴더에 설치하므로)."""
	inside = tree.root / HOME[1:] / 'lekiwi_net'
	shutil.copytree(ROBOT_NET, inside, ignore=shutil.ignore_patterns('tests', '.git*', '__pycache__'))
	before = tree_state(tree.root)
	done = tree.fake.run([str(inside / 'provision_ap_mode.sh'), '--kit', '4'], **tree.env())
	assert done.returncode == 3, done.stdout + done.stderr
	assert 'robot_net_staging' in done.stderr
	assert tree_state(tree.root) == before
	assert not [line for line in tree.calls() if line.startswith(MUTATING)]


def test_provision_dry_run_changes_nothing(tree: RobotTree) -> None:
	before = tree_state(tree.root)
	done = ok(tree.provision('--dry-run', '--kit', '4'))
	assert tree_state(tree.root) == before
	assert 'DRY-RUN' in done.stdout
	assert done.stdout.rstrip().splitlines()[-1] == 'DRY-RUN: 변경 없음'
	for step in range(1, 10):
		assert f'[provision] {step}/9 ' in done.stdout
	assert not [line for line in tree.calls() if line.startswith(MUTATING)]
	assert ROUTER_PSK not in done.stdout + done.stderr


def test_provision_removes_wifis_keeps_eth0(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4'))
	assert tree.text('etc/netplan/50-cloud-init.yaml') == NETPLAN_WITHOUT_WIFIS
	assert (tree.root / 'etc/netplan/50-cloud-init.yaml').stat().st_mode & 0o777 == 0o600


def test_provision_router_psk_goes_to_keyfile_only(tree: RobotTree) -> None:
	done = ok(tree.provision('--kit', '4'))
	assert tree.keyfile('home-wifi').get('wifi-security', 'psk') == ROUTER_PSK
	assert tree.keyfile('home-wifi').get('wifi', 'ssid') == 'roboseasy'
	assert ROUTER_PSK not in done.stdout + done.stderr
	[backup] = tree.backups()
	assert backup.stat().st_mode & 0o777 == 0o600
	with tarfile.open(backup) as archive:
		names = archive.getnames()
		holders = [member.name for member in archive.getmembers() if member.isfile()
			and ROUTER_PSK.encode() in archive.extractfile(member).read()]
	assert 'etc/netplan/50-cloud-init.yaml' in holders and 'boot/firmware/network-config' in holders
	for rel in ('etc/netplan/.50-cloud-init.yaml.bak', 'etc/kit-number', 'etc/systemd/system/wifi-powersave-off.service',
		'etc/systemd/system/multi-user.target.wants/wifi-powersave-off.service',
		'etc/networkd-dispatcher/routable.d/50-kit-ip', f'{HOME[1:]}/.bashrc', f'{HOME[1:]}/set_wifi.sh',
		f'{HOME[1:]}/wifi_list.sh', f'{HOME[1:]}/clone_init.sh'):
		assert rel in names


def test_provision_default_ap_is_lekiwi01(tree: RobotTree) -> None:
	"""처음 AP는 모든 로봇이 이름 lekiwi01 · 비밀번호 lekiwi01 (사용자 결정, R25). 고객이 바꾼다."""
	done = ok(tree.provision('--kit', '4'))
	ap = tree.keyfile('lekiwi-ap')
	assert (ap.get('wifi', 'ssid'), ap.get('wifi-security', 'psk')) == ('lekiwi01', 'lekiwi01')
	assert not (tree.root / 'run/lekiwi-ap-password').exists()
	assert 'lekiwi-ap-password' not in done.stdout


@pytest.mark.parametrize('name', ['lekiwi-lab', 'a' * 32])
def test_provision_ap_ssid_option_sets_name_and_password(tree: RobotTree, name: str) -> None:
	ok(tree.provision('--kit', '4', '--ap-ssid', name))
	ap = tree.keyfile('lekiwi-ap')
	assert (ap.get('wifi', 'ssid'), ap.get('wifi-security', 'psk')) == (name, name)


def keyfile_items(text: str) -> set[tuple[str, str, str]]:
	parser = configparser.RawConfigParser(delimiters=('=',), interpolation=None)
	parser.optionxform = str
	parser.read_string(text)
	return {(section, key, value) for section in parser.sections() for key, value in parser.items(section)}


def nmcli_reference(*args: str) -> str:
	"""이 PC의 실제 nmcli를 데몬 없이(--offline) 돌려 keyfile 기준 출력을 얻는다."""
	return subprocess.run([shutil.which('nmcli'), '--offline', 'connection', 'add', 'type', 'wifi', 'ifname', 'wlan0',
		*args], capture_output=True, text=True, check=True, env={'LANG': 'C'}).stdout


def nmcli_offline_ok() -> bool:
	if not shutil.which('nmcli'):
		return False
	version = subprocess.run([shutil.which('nmcli'), '--version'], capture_output=True, text=True,
		env={'LANG': 'C'}).stdout
	found = re.search(r'(\d+)\.(\d+)\.\d+', version)
	return bool(found) and (int(found[1]), int(found[2])) >= (1, 46)


@pytest.mark.skipif(not nmcli_offline_ok(), reason='nmcli --offline (1.46 이상)이 없다')
def test_keyfile_templates_match_nmcli_offline(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4'))
	ap_password = 'lekiwi01'                                          # 처음 AP: 이름과 같은 공개 기본값
	references = {
		'lekiwi-ap': nmcli_reference('con-name', 'lekiwi-ap', 'ssid', 'lekiwi01', '802-11-wireless.mode', 'ap',
			'802-11-wireless.band', 'bg', '802-11-wireless.channel', '6', 'wifi-sec.key-mgmt', 'wpa-psk',
			'wifi-sec.proto', 'rsn', 'wifi-sec.pairwise', 'ccmp', 'wifi-sec.group', 'ccmp', 'wifi-sec.psk', ap_password,
			'ipv4.method', 'shared', 'ipv4.addresses', '192.168.4.1/24', 'ipv6.method', 'disabled',
			'connection.autoconnect-priority', '0'),
		'home-wifi': nmcli_reference('con-name', 'home-wifi', 'ssid', 'roboseasy', '802-11-wireless.mode',
			'infrastructure', 'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', ROUTER_PSK, 'ipv4.method', 'manual',
			'ipv4.addresses', '192.168.0.204/24', 'ipv4.gateway', '192.168.0.1', 'ipv4.dns', '8.8.8.8 1.1.1.1',
			'ipv6.method', 'disabled', 'connection.autoconnect-priority', '10'),
	}
	defaults = {('connection', 'autoconnect', 'true'), ('connection', 'autoconnect-priority', '0')}
	for name, reference in references.items():
		ours = keyfile_items(tree.text(f'etc/NetworkManager/system-connections/{name}.nmconnection'))
		theirs = keyfile_items(reference)
		assert [item for item in ours if item[:2] == ('connection', 'uuid')]        # uuid 값은 서로 다르다
		ours = {item for item in ours if item[:2] != ('connection', 'uuid')}
		theirs = {item for item in theirs if item[:2] not in (('connection', 'uuid'), ('ipv6', 'addr-gen-mode'))}
		assert ours - defaults <= theirs, name
		assert theirs <= ours, name


def test_provision_holds_wlan0_before_install(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4', '--hold-nm'))
	calls = tree.calls()
	install = calls[index_of(calls, 'apt-get install')]
	# Recommends(ppp · pptp 등)는 빼고 AP 공유 모드에 필요한 dnsmasq-base만 더한다
	assert install.startswith('apt-get install -y --no-install-recommends network-manager dnsmasq-base |')
	assert 'hold: [keyfile] unmanaged-devices=*' in install
	assert index_of(calls, 'apt-get install') < index_of(calls, 'systemctl enable --now NetworkManager') < index_of(
		calls, 'nmcli -t -f DEVICE,STATE dev')
	assert 'apt-mark hold network-manager' in calls
	assert not (tree.root / 'etc/NetworkManager/conf.d/90-lekiwi-hold.conf').exists()     # 옮길 때 지운다
	assert tree.text('etc/NetworkManager/conf.d/default-wifi-powersave-on.conf') == '[connection]\nwifi.powersave = 2\n'
	assert tree.text('etc/modprobe.d/cfg80211-kr.conf') == 'options cfg80211 ieee80211_regdom=KR\n'


NETWORKMANAGER = Path('/usr/sbin/NetworkManager')


@pytest.mark.skipif(not NETWORKMANAGER.exists(), reason='NetworkManager가 이 PC에 없다')
def test_hold_file_wins_in_networkmanager_merge(tmp_path: Path) -> None:
	"""NetworkManager 자신의 설정 읽기(--print-config, 데몬을 띄우지 않는다)로 확인한다: 붙잡는 파일이 Ubuntu 기본값
	(/usr/lib)과, 같은 /etc/conf.d 안에서 이름이 앞서는 다른 조각을 이긴다."""
	hold = re.search(r'conf\.d/([^"/]+)"', PROVISION.read_text())[1]
	assert f'conf.d/{hold}"' in ROLLBACK.read_text()
	assert '[keyfile]\nunmanaged-devices=*\nEOF' in PROVISION.read_text()
	lib, etc = tmp_path / 'lib', tmp_path / 'etc'
	lib.mkdir()
	etc.mkdir()
	# 이름은 이 PC의 /run/NetworkManager/conf.d(같은 이름이면 가린다)와 겹치지 않게
	(lib / '10-globally-managed-devices-robot.conf').write_text(
		'[keyfile]\nunmanaged-devices=*,except:type:wifi,except:type:gsm,except:type:cdma\n')
	(etc / '10-other.conf').write_text('[keyfile]\nunmanaged-devices=interface-name:eth9\n')
	(etc / hold).write_text('[keyfile]\nunmanaged-devices=*\n')
	(tmp_path / 'NetworkManager.conf').write_text('[main]\n')
	(tmp_path / 'intern.conf').write_text('')
	merged = subprocess.run([str(NETWORKMANAGER), '--print-config', f'--config={tmp_path / "NetworkManager.conf"}',
		f'--config-dir={etc}', f'--system-config-dir={lib}', f'--intern-config={tmp_path / "intern.conf"}'],
		capture_output=True, text=True, env={'LANG': 'C'}, timeout=30).stdout
	assert [line for line in merged.splitlines() if line.startswith('unmanaged-devices=')] == ['unmanaged-devices=*'], merged


def test_provision_waits_for_networkmanager_to_manage_wlan0(tmp_path: Path) -> None:
	tree = RobotTree(tmp_path, {'sleep': 'echo "sleep $*" >> "$FAKE_LOG"\n'})
	# 설치 뒤 확인 1번 + 다시 켠 뒤 두 번은 아직 unmanaged, 그다음 disconnected
	ok(tree.provision('--kit', '4', FAKE_WLAN0_STATES='unmanaged unmanaged unmanaged disconnected'))
	calls = tree.calls()
	restart = index_of(calls, 'systemctl restart NetworkManager')
	after = calls[restart:]
	assert sum(line == 'nmcli -t -f DEVICE,STATE dev' for line in after) == 3
	assert after.index('nmcli -t -f DEVICE,STATE dev') < index_of(after, 'sleep') < index_of(after, 'con up home-wifi')


def test_provision_rolls_back_when_wlan0_stays_unmanaged(tmp_path: Path) -> None:
	tree = RobotTree(tmp_path, {'sleep': 'exit 0\n'})
	done = tree.provision('--kit', '4', FAKE_WLAN0_STATES='unmanaged')
	assert done.returncode == 4
	assert 'wlan0' in done.stderr
	assert tree.text('etc/netplan/50-cloud-init.yaml') == NETPLAN
	assert not [line for line in tree.calls() if 'con up' in line]


@pytest.mark.parametrize('args, netplan', [
	(('--kit', '4'), NETPLAN.replace('"roboseasy":', '"robo;seasy":')),
	(('--kit', '4', '--ap-ssid', 'lekiwi;04'), NETPLAN),
], ids=['router', 'ap'])
def test_provision_refuses_semicolon_in_ssid(tree: RobotTree, args: tuple[str, ...], netplan: str) -> None:
	"""keyfile의 ssid=는 ;를 목록 구분자로 본다 (nm_kf.py는 \\ 만 두 배로 쓴다)."""
	(tree.root / 'etc/netplan/50-cloud-init.yaml').write_text(netplan)
	before = tree_state(tree.root)
	done = tree.provision(*args)
	assert done.returncode == 3
	assert ';' in done.stderr
	assert tree_state(tree.root) == before


@pytest.mark.parametrize('env', [{'FAKE_WLAN0_STATES': 'disconnected'}, {'FAKE_ETH0_STATE': 'connected'}])
def test_provision_rolls_back_when_networkmanager_grabs_a_device(tmp_path: Path, env: dict[str, str]) -> None:
	tree = RobotTree(tmp_path, {'sleep': 'exit 0\n'})
	done = tree.provision('--kit', '4', **env)
	assert done.returncode == 4
	assert tree.text('etc/netplan/50-cloud-init.yaml') == NETPLAN
	assert 'systemctl disable --now NetworkManager' in tree.calls()
	assert 'netplan apply' not in tree.calls()                    # netplan을 바꾸기 전이라 다시 적용하지 않는다


def test_provision_stops_netplan_wpa_unit_before_networkmanager_takes_wlan0(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4'))
	calls = tree.calls()
	assert index_of(calls, 'netplan apply') < index_of(calls, 'systemctl stop netplan-wpa-wlan0.service') < index_of(
		calls, 'systemctl restart NetworkManager') < index_of(calls, 'nmcli con load')


def test_provision_rolls_back_when_netplan_wpa_unit_keeps_running(tree: RobotTree) -> None:
	done = tree.provision('--kit', '4', FAKE_ACTIVE_UNITS='netplan-wpa-wlan0.service')
	assert done.returncode == 4
	assert 'netplan-wpa-wlan0' in done.stderr
	assert tree.text('etc/netplan/50-cloud-init.yaml') == NETPLAN
	assert not (tree.root / 'etc/NetworkManager/system-connections/home-wifi.nmconnection').exists()
	assert not [line for line in tree.calls() if line.startswith('nmcli con load')]


def test_provision_neutralizes_legacy(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4'))
	for tool in ('set_wifi.sh', 'set_ip.sh', 'set_dhcp.sh'):
		assert not (tree.home / tool).exists()
		assert (tree.home / f'{tool}.legacy-netplan').exists()
	assert not (tree.home / 'wifi_list.sh').exists()                      # ~/lekiwi_net/으로 옮겼다 (아래 반복문)
	assert not (tree.root / 'etc/networkd-dispatcher/routable.d/50-kit-ip').exists()
	assert 'wifis' not in tree.text('boot/firmware/network-config')
	assert ROUTER_PSK not in tree.text('boot/firmware/network-config')
	assert 'eth0:' in tree.text('boot/firmware/network-config')
	assert tree.text('etc/kit-number') == '4\n'
	assert 'systemctl disable wifi-powersave-off.service' in tree.calls()
	for script in ('nm_kf.py', 'run_ap.sh', 'run_wifi.sh', 'set_ap.sh', 'net_status.sh', 'set_ip.sh', 'set_dhcp.sh',
		'_apply_home_ipv4.sh', 'wifi_list.sh'):
		installed = tree.home / 'lekiwi_net' / script
		assert installed.read_bytes() == (ROBOT_NET / script).read_bytes()
		assert installed.stat().st_mode & 0o777 == 0o755


def test_provision_keeps_home_wifi_list_while_bashrc_still_calls_it(tree: RobotTree) -> None:
	"""벤더 표지 블록이 이미 있으면 4단계는 .bashrc를 건드리지 않는다. 그 블록이 옛 경로를 부르는 동안은 홈 파일을 지우지 않는다."""
	vendor_block = '# >>> lekiwi_net >>>\nwifi() { "$HOME/wifi_list.sh" "${2:-}"; }\n# <<< lekiwi_net <<<\n'
	(tree.home / '.bashrc').write_text(OLD_BASHRC + vendor_block)
	ok(tree.provision('--kit', '4'))
	assert (tree.home / 'wifi_list.sh').exists()
	assert (tree.home / 'lekiwi_net' / 'wifi_list.sh').exists()
	assert tree.text(f'{HOME[1:]}/.bashrc') == OLD_BASHRC + vendor_block


@pytest.mark.parametrize('kind', ['directory', 'broken-link'])
def test_provision_leaves_odd_home_wifi_list_alone(tree: RobotTree, kind: str) -> None:
	"""폴더는 rm이 실패해 이관 전체가 되돌려지고, 끊긴 링크는 백업에 들지 않는다: 둘 다 지우지 않고 이관은 끝낸다."""
	odd = tree.home / 'wifi_list.sh'
	odd.unlink()
	if kind == 'directory':
		odd.mkdir()
	else:
		odd.symlink_to('/nonexistent/target')
	ok(tree.provision('--kit', '4'))
	assert odd.is_dir() if kind == 'directory' else odd.is_symlink()
	assert (tree.root / 'etc/lekiwi-net-provisioned').exists()


def test_provision_appends_bashrc_block_once(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4'))
	bashrc = tree.text(f'{HOME[1:]}/.bashrc')
	assert bashrc.startswith(OLD_BASHRC)
	assert bashrc.count('# >>> lekiwi_net >>>') == 1 and bashrc.count('# <<< lekiwi_net <<<') == 1
	assert bashrc.endswith((ROBOT_NET / 'bashrc_net.sh').read_text())
	assert '"$HOME/lekiwi_net/run_wifi.sh" "$@"' in bashrc
	assert '"$HOME/lekiwi_net/wifi_list.sh" "${2:-}"' in bashrc and '"$HOME/wifi_list.sh"' not in bashrc


@pytest.mark.parametrize('args, address', [
	(('--kit', '4'), '192.168.0.204/24,192.168.0.1'),
	(('--kit', '7'), '192.168.0.207/24,192.168.0.1'),
	((), '192.168.0.204/24,192.168.0.1'),                     # /etc/kit-number
	(('--dhcp',), None),
])
def test_provision_fixed_ip_from_kit(tree: RobotTree, args: tuple[str, ...], address: str | None) -> None:
	ok(tree.provision(*args))
	home = tree.keyfile('home-wifi')
	assert home.get('ipv4', 'address1', fallback=None) == address
	assert home.get('ipv4', 'method') == ('manual' if address else 'auto')
	assert home.get('ipv4', 'dns', fallback=None) == ('8.8.8.8;1.1.1.1;' if address else None)
	assert home.get('connection', 'autoconnect') == 'true'


def test_provision_ap_mode(tree: RobotTree) -> None:
	ok(tree.provision('--mode', 'ap', FAKE_ACTIVE='lekiwi-ap'))
	assert tree.keyfile('home-wifi').get('connection', 'autoconnect') == 'false'
	assert tree.keyfile('lekiwi-ap').get('wifi', 'ssid') == 'lekiwi01'
	assert 'nmcli con up lekiwi-ap' in tree.calls()


@pytest.mark.parametrize('args', [
	('--mode', 'x'), ('--kit', '0'), ('--kit', '55'), ('--kit', 'a'), ('--kit', '4', '--dhcp'), ('--ap-ssid', ''),
	('--ap-ssid', 'a' * 33), ('--ap-ssid', ' lekiwi04'), ('--ap-ssid', 'a\nb'), ('--bogus',), ('--kit',),
	('--ap-ssid', 'lekiwi7'), ('--ap-ssid', '로봇이름입니다'),                 # 비밀번호도 되므로 8자 이상 ASCII
])
def test_provision_rejects_bad_arguments(tree: RobotTree, args: tuple[str, ...]) -> None:
	before = tree_state(tree.root)
	assert tree.provision(*args).returncode == 3
	assert tree_state(tree.root) == before


@pytest.mark.parametrize('password_line', [
	'          password: "FAKE\\\\PSK-1"',       # YAML 이스케이프가 든 값
	'          password: FAKEPSK-12345',         # 따옴표 없음
])
def test_provision_refuses_unreadable_router_credentials(tree: RobotTree, password_line: str) -> None:
	netplan = tree.root / 'etc/netplan/50-cloud-init.yaml'
	netplan.write_text(NETPLAN.replace('          password: "FAKEPSK-12345"', password_line))
	before = tree_state(tree.root)
	done = tree.provision('--kit', '4')
	assert done.returncode == 3
	assert tree_state(tree.root) == before
	assert 'PSK' not in done.stdout + done.stderr


def test_provision_arms_rollback_timer(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4'))
	calls = tree.calls()
	timer = calls[index_of(calls, 'systemd-run')]
	[backup] = tree.backups()
	assert '--on-active=180' in timer and '--unit=lekiwi-net-rollback' in timer
	assert timer.endswith(f'{ROLLBACK} {backup}')
	assert index_of(calls, 'systemd-run') < index_of(calls, 'netplan apply')


def test_provision_failure_after_switch_rolls_back(tree: RobotTree) -> None:
	done = tree.provision('--kit', '4', FAKE_NETPLAN_FAIL_ON='2')        # 1번째 generate, 2번째 apply가 실패
	assert done.returncode == 4
	assert tree.text('etc/netplan/50-cloud-init.yaml') == NETPLAN
	assert tree.text('boot/firmware/network-config') == NETWORK_CONFIG
	assert not list((tree.root / 'etc/NetworkManager/system-connections').glob('*.nmconnection'))
	assert not (tree.root / 'run/lekiwi-ap-password').exists()
	assert not (tree.root / 'etc/lekiwi-net-provisioned').exists()
	assert 'systemctl stop lekiwi-net-rollback.timer' in tree.calls()


def test_provision_rolls_back_when_wlan0_does_not_come_up(tmp_path: Path) -> None:
	tree = RobotTree(tmp_path, {'sleep': 'exit 0\n'})                    # 60초 확인을 기다리지 않는다
	done = tree.provision('--kit', '4', FAKE_ACTIVE='lekiwi-ap')          # 공유기 대신 AP가 떠 있다
	assert done.returncode == 4
	assert tree.text('etc/netplan/50-cloud-init.yaml') == NETPLAN
	assert (tree.home / 'set_wifi.sh').exists() and not (tree.home / 'set_wifi.sh.legacy-netplan').exists()
	assert (tree.home / 'wifi_list.sh').exists() and not (tree.home / 'lekiwi_net/wifi_list.sh').exists()   # 옮긴 것도 되돌린다


def test_provision_is_idempotent(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4'))
	assert (tree.root / 'etc/lekiwi-net-provisioned').exists()
	calls = len(tree.calls())
	again = ok(tree.provision('--kit', '4'))
	assert '이미 적용됨' in again.stdout
	assert len(tree.calls()) == calls
	assert tree.text(f'{HOME[1:]}/.bashrc').count('# >>> lekiwi_net >>>') == 1


def test_rollback_restores_files(tree: RobotTree) -> None:
	skip = ('etc/NetworkManager', 'var/backups')                          # 패키지 폴더 · 백업 자신
	before = tree_state(tree.root, skip)
	ok(tree.provision('--kit', '4'))
	assert tree_state(tree.root, skip) != before
	done = ok(tree.rollback())
	assert tree_state(tree.root, skip) == before, done.stdout
	assert not list((tree.root / 'etc/NetworkManager/system-connections').glob('*.nmconnection'))
	assert tree.text('etc/NetworkManager/conf.d/90-lekiwi-hold.conf') == '[keyfile]\nunmanaged-devices=*\n'
	calls = rollback_calls(tree)
	assert 'systemctl disable --now NetworkManager' in calls
	assert 'netplan apply' in calls                                    # 롤백이 netplan을 다시 적용했다
	assert calls[-1] == 'systemctl is-active --quiet netplan-wpa-wlan0.service'


def rollback_calls(tree: RobotTree) -> list[str]:
	"""마지막 롤백이 시작된 뒤의 stub 호출만 (프로비저닝의 호출은 뺀다)."""
	calls = tree.calls()
	start = max(i for i, line in enumerate(calls) if line == 'systemctl stop lekiwi-net-rollback.timer')
	return calls[start:]


def test_rollback_restores_files_before_stopping_networkmanager(tmp_path: Path) -> None:
	tar = shutil.which('tar')
	tree = RobotTree(tmp_path, {'tar': f'echo "tar $*" >> "$FAKE_LOG"\nexec {tar} "$@"\n'})
	ok(tree.provision('--kit', '4'))
	ok(tree.rollback())
	calls = rollback_calls(tree)
	order = [index_of(calls, 'tar -xpzf'), index_of(calls, 'nmcli -w 10 dev disconnect wlan0'),
		index_of(calls, 'systemctl disable --now NetworkManager'), index_of(calls, "pkill -f dnsmasq.*nm-dnsmasq-wlan0"),
		index_of(calls, 'netplan apply')]
	assert order == sorted(order), calls


def test_rollback_stops_when_files_cannot_be_restored(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4'))
	netplan_dir = tree.root / 'etc/netplan'
	netplan_dir.chmod(0o555)                                           # 풀기가 실패한다
	try:
		done = tree.rollback()
	finally:
		netplan_dir.chmod(0o755)
	assert done.returncode == 1
	calls = rollback_calls(tree)
	assert not [line for line in calls if 'NetworkManager' in line or line.startswith(('netplan', 'nmcli'))]
	assert (tree.root / 'etc/NetworkManager/system-connections/home-wifi.nmconnection').exists()   # NM이 계속 맡는다
	assert not (tree.root / 'etc/NetworkManager/conf.d/90-lekiwi-hold.conf').exists()
	assert '멈춘다' in done.stdout


def test_rollback_twice_is_harmless(tree: RobotTree) -> None:
	skip = ('etc/NetworkManager', 'var/backups')
	ok(tree.provision('--kit', '4'))
	ok(tree.rollback())
	once = tree_state(tree.root, skip)
	ok(tree.rollback())
	assert tree_state(tree.root, skip) == once
	assert 'netplan apply' not in rollback_calls(tree)                 # netplan이 이미 백업과 같다


def test_reprovision_after_rollback(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4'))
	ok(tree.rollback())
	tree.reset_device_states()
	ok(tree.provision('--kit', '4'))
	assert (tree.root / 'etc/lekiwi-net-provisioned').exists()
	assert tree.keyfile('home-wifi').get('wifi-security', 'psk') == ROUTER_PSK
	assert tree.text('etc/netplan/50-cloud-init.yaml') == NETPLAN_WITHOUT_WIFIS
	assert not (tree.root / 'etc/NetworkManager/conf.d/90-lekiwi-hold.conf').exists()


def test_rollback_after_failed_install_does_not_reapply_netplan(tree: RobotTree) -> None:
	done = tree.provision('--kit', '4', FAKE_APT_FAILS='1')
	assert done.returncode == 4
	assert tree.text('etc/netplan/50-cloud-init.yaml') == NETPLAN
	assert not [line for line in tree.calls() if line.startswith('netplan')]     # eth0을 다시 건드리지 않는다
	assert 'systemctl disable --now NetworkManager' in tree.calls()


def test_rollback_fails_when_netplan_does_not_take_wlan0_back(tree: RobotTree) -> None:
	ok(tree.provision('--kit', '4'))
	done = tree.rollback(FAKE_WPA_STAYS_DOWN='1')
	assert done.returncode == 1
	assert 'netplan-wpa-wlan0' in done.stdout


def test_rollback_without_backup_changes_nothing(tree: RobotTree) -> None:
	before = tree_state(tree.root)
	done = tree.rollback()
	assert done.returncode == 1
	assert tree_state(tree.root) == before
	assert not tree.calls()


@pytest.mark.parametrize('sig', [signal.SIGTERM, signal.SIGHUP, signal.SIGINT], ids=['TERM', 'HUP', 'INT'])
def test_provision_rolls_back_when_killed_by_signal(tree: RobotTree, sig: signal.Signals) -> None:
	"""ssh가 끊기거나(HUP) 누가 멈춰도(TERM · INT) 바꾼 뒤라면 롤백한다."""
	proc = tree.fake.start([str(PROVISION), '--kit', '4'], **tree.env(FAKE_NETPLAN_BLOCK='generate'))
	deadline = time.monotonic() + 20
	while 'netplan generate' not in tree.calls():
		assert proc.poll() is None and time.monotonic() < deadline, proc.communicate()
		time.sleep(0.05)
	os.killpg(proc.pid, sig)
	out, err = proc.communicate(timeout=60)
	assert proc.returncode == 4, out + err
	assert tree.text('etc/netplan/50-cloud-init.yaml') == NETPLAN
	assert not list((tree.root / 'etc/NetworkManager/system-connections').glob('*.nmconnection'))
	assert '[rollback] 끝' in out


def test_provision_reports_failed_rollback_with_exit_5(tree: RobotTree) -> None:
	done = tree.provision('--kit', '4', FAKE_NETPLAN_FAIL_ON='2 3')     # 옮길 때의 apply, 롤백의 apply 둘 다 실패
	assert done.returncode == 5, done.stdout + done.stderr
	assert '롤백도 실패' in done.stderr


def test_provision_names_backup_after_rollback(tree: RobotTree) -> None:
	done = tree.provision('--kit', '4', FAKE_NETPLAN_FAIL_ON='2')
	assert done.returncode == 4
	[backup] = tree.backups()
	assert f'/var/backups/{backup.name}' in done.stderr           # 공유기 비밀번호가 든 사본: 옮긴 뒤 지울 것
	assert ROUTER_PSK not in done.stdout + done.stderr


def test_provision_creates_networkmanager_folders_readable(tree: RobotTree) -> None:
	"""umask 077 아래에서도 /etc/NetworkManager · conf.d는 패키지처럼 755 (설치 · 다른 도구가 읽는다)."""
	ok(tree.provision('--kit', '4'))
	for rel in ('etc/NetworkManager', 'etc/NetworkManager/conf.d'):
		assert (tree.root / rel).stat().st_mode & 0o777 == 0o755, rel
	install = tree.calls()[index_of(tree.calls(), 'apt-get install')]
	assert install.endswith('umask 0022 NEEDRESTART_SUSPEND=1')         # 패키지 스크립트가 만드는 파일 권한 · 서비스 재시작 없음


@pytest.mark.parametrize('old, new', [
	('"roboseasy":', '" roboseasy":'),
	('"roboseasy":', '"roboseasy ":'),
	('password: "FAKEPSK-12345"', 'password: "FAKEPSK-12345 "'),
	('password: "FAKEPSK-12345"', 'password: " FAKEPSK-12345"'),
], ids=['ssid-head', 'ssid-tail', 'psk-tail', 'psk-head'])
def test_provision_refuses_router_values_with_edge_spaces(tree: RobotTree, old: str, new: str) -> None:
	"""keyfile은 값의 앞뒤 공백을 잘라 읽어, 옮기면 다른 이름 · 비밀번호가 된다."""
	(tree.root / 'etc/netplan/50-cloud-init.yaml').write_text(NETPLAN.replace(old, new))
	before = tree_state(tree.root)
	done = tree.provision('--kit', '4')
	assert done.returncode == 3
	assert '공백' in done.stderr and 'FAKEPSK' not in done.stderr
	assert tree_state(tree.root) == before


@pytest.mark.parametrize('rel, base, extra', [
	('etc/netplan/50-cloud-init.yaml', NETPLAN, '  extra:\n    access-points: {}\n'),
	('boot/firmware/network-config', NETWORK_CONFIG, '  extra:\n    "cafe":\n      password: "FAKEPSK-9"\n'),
], ids=['netplan', 'network-config'])
def test_provision_refuses_when_secrets_survive_wifis_removal(tree: RobotTree, rel: str, base: str, extra: str) -> None:
	"""wifis 블록 밖에도 비밀번호 · access-points 줄이 있으면 (모르는 모양) 지운 결과를 믿지 않는다."""
	(tree.root / rel).write_text(base + extra)
	before = tree_state(tree.root)
	done = tree.provision('--kit', '4')
	assert done.returncode == 3, done.stdout + done.stderr
	assert 'FAKEPSK' not in done.stdout + done.stderr
	assert tree_state(tree.root) == before


def test_provision_tells_remaining_timer_seconds(tree: RobotTree) -> None:
	done = ok(tree.provision('--kit', '4'))
	found = re.search(r'남은 약 (\d+)초', done.stdout)
	assert found and 0 < int(found[1]) <= 180, done.stdout


def test_provision_does_not_mark_done_when_timer_already_ran(tree: RobotTree) -> None:
	"""타이머가 이미 돌았으면(롤백이 맡음) 표지를 쓰지 않고, 두 번째 롤백을 겹쳐 돌리지 않는다."""
	done = tree.provision('--kit', '4', FAKE_TIMER_GONE='1')
	assert done.returncode == 4
	assert not (tree.root / 'etc/lekiwi-net-provisioned').exists()
	assert '[rollback]' not in done.stdout and '타이머' in done.stderr
	assert tree.backups()                                              # 타이머의 롤백이 쓸 백업은 남긴다


def test_provision_zeroes_old_network_config_before_rewrite(tmp_path: Path) -> None:
	"""vfat에서 제자리에 다시 쓰면 옛 클러스터(공유기 비밀번호)가 빈 자리로 남는다: 같은 길이의 0으로 먼저 덮는다."""
	head = shutil.which('head')
	tree = RobotTree(tmp_path, {'head': f'echo "head $*" >> "$FAKE_LOG"\nexec {head} "$@"\n',
		'sync': 'echo "sync $*" >> "$FAKE_LOG"\n'})
	ok(tree.provision('--kit', '4'))
	calls = tree.calls()
	netcfg = tree.root / 'boot/firmware/network-config'
	zero = index_of(calls, f'head -c {len(NETWORK_CONFIG.encode())} /dev/zero')
	syncs = [i for i, line in enumerate(calls) if line == f'sync {netcfg}']
	assert len(syncs) == 2 and zero < syncs[0]                   # 0으로 덮고 sync, 새 내용을 쓰고 sync
	assert 'wifis' not in netcfg.read_text() and '\0' not in netcfg.read_text()
