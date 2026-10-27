"""lekiwi_net 스크립트 시험 (nm_kf.py · 전환 스크립트).

이 PC의 네트워크를 건드리지 않도록 스크립트는 가짜 PATH 하나에서만 돈다: stub(`sudo` · `nmcli` · `pkill` ·
`ip`, 호출을 기록만 한다)과 실제로 써도 안전한 도구의 심볼릭 링크뿐이다. stub이 없는 시스템 명령은
"command not found"로 실패한다. keyfile 폴더 · 상태 폴더는 환경변수로 임시 폴더를 가리킨다.
"""

import configparser
import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROBOT_NET = Path(__file__).resolve().parent.parent   # 이 저장소 = 로봇의 ~/lekiwi_net

# 실제 것을 그대로 쓰는 도구 (읽기 · 주어진 경로의 파일 다루기만). 이 밖의 명령은 가짜 PATH에 없다.
SAFE_TOOLS: tuple[str, ...] = ('bash', 'python3', 'grep', 'sed', 'awk', 'tr', 'wc', 'cat', 'cut', 'cp', 'mv',
	'rm', 'rmdir', 'mkdir', 'install', 'chmod', 'chown', 'test', 'date', 'readlink', 'dirname', 'basename', 'head',
	'tail', 'sort', 'find', 'sleep', 'tar', 'gzip', 'id', 'sync')

# 기본 stub (bash 본문). 모두 호출 인자를 $FAKE_LOG에 한 줄로 남긴다.
STUBS: dict[str, str] = {
	# 남은 인자를 그대로 실행한다 (-n 은 버린다).
	'sudo': '''echo "sudo $*" >> "$FAKE_LOG"
[ "${1:-}" = -n ] && shift
exec "$@"
''',
	# FAKE_ACTIVE: `con show --active`에 보일 프로필 이름(공백으로 여럿). FAKE_HOME_UP_FAILS=1 · FAKE_AP_UP_FAILS=1:
	# 그 프로필의 `con up` 실패. FAKE_ETH0_STATE: `dev`의 eth0 상태 (기본 unmanaged). FAKE_WLAN0_STATES: `dev`를
	# 부를 때마다 차례로 보일 wlan0 상태 (공백으로 여럿, 마지막 것이 이어진다. 기본 unmanaged).
	'nmcli': '''echo "nmcli $*" >> "$FAKE_LOG"
fields=NAME; prev=''
for a in "$@"; do [ "$prev" = -f ] && fields=$a; prev=$a; done
case " $* " in
*' con show --active '*)
	for name in ${FAKE_ACTIVE:-}; do
		out=''
		for f in ${fields//,/ }; do
			case $f in NAME) v=$name ;; DEVICE) v=wlan0 ;; STATE) v=activated ;; *) v='' ;; esac
			out=${out:+$out:}$v
		done
		echo "$out"
	done ;;
*' con up home-wifi '*) [ "${FAKE_HOME_UP_FAILS:-0}" != 1 ] || { echo 'Error: fake: home-wifi up failed' >&2; exit 4; } ;;
*' con up lekiwi-ap '*) [ "${FAKE_AP_UP_FAILS:-0}" != 1 ] || { echo 'Error: fake: lekiwi-ap up failed' >&2; exit 4; } ;;
*' dev disconnect '*) ;;
*' dev '*)
	n=$(( $(cat "$FAKE_LOG.dev" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_LOG.dev"
	set -- ${FAKE_WLAN0_STATES:-unmanaged}
	[ "$n" -le $# ] || n=$#
	printf 'eth0:%s\\nwlan0:%s\\n' "${FAKE_ETH0_STATE:-unmanaged}" "${!n}" ;;
esac
exit 0
''',
	'pkill': '''echo "pkill $*" >> "$FAKE_LOG"
''',
	# FAKE_ETH0_ADDR · FAKE_WLAN0_ADDR: 주소 (빈 값이면 주소 없음). FAKE_ETH0_CARRIER=0: 케이블 없음.
	'ip': '''echo "ip $*" >> "$FAKE_LOG"
dev=''
for a in "$@"; do case $a in eth0|wlan0) dev=$a ;; esac; done
case " $* " in
*' link '*)
	[ "$dev" = wlan0 ] && exit 0
	if [ "${FAKE_ETH0_CARRIER:-1}" = 1 ]; then
		echo '2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default qlen 1000'
	else
		echo '2: eth0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500 qdisc mq state DOWN mode DEFAULT group default qlen 1000'
	fi ;;
*)
	for d in eth0 wlan0; do
		[ -z "$dev" ] || [ "$dev" = "$d" ] || continue
		if [ "$d" = eth0 ]; then addr=${FAKE_ETH0_ADDR-10.42.0.195/24}; else addr=${FAKE_WLAN0_ADDR-192.168.0.204/24}; fi
		[ -n "$addr" ] || continue
		case " $* " in
		*' -br '*) echo "$d UP $addr" ;;
		*) echo "3: $d    inet $addr brd x scope global $d" ;;
		esac
	done ;;
esac
exit 0
''',
}


class FakeBin:
	"""가짜 PATH 하나(stub + 안전한 실제 도구)와 그 호출 기록.

	Args:
		base: 이 시험의 임시 폴더. `fake_bin/` · `calls.log` · `fake_home/`을 여기에 만든다.
		extra_stubs: 더할 stub (이름 → bash 본문). 같은 이름이면 기본 stub을 덮는다.
	"""

	def __init__(self, base: Path, extra_stubs: dict[str, str] | None = None) -> None:
		self.bin = base / 'fake_bin'
		self.bin.mkdir()
		self.log = base / 'calls.log'
		stubs = {**STUBS, **(extra_stubs or {})}
		for name, body in stubs.items():
			path = self.bin / name
			path.write_text('#!/bin/bash\n' + body)
			path.chmod(0o755)
		for tool in SAFE_TOOLS:
			if tool in stubs:
				continue
			real = shutil.which(tool)
			assert real, f'{tool}이 이 PC에 없다'
			(self.bin / tool).symlink_to(real)
		home = base / 'fake_home'
		home.mkdir()
		self.env = {'PATH': str(self.bin), 'FAKE_LOG': str(self.log), 'HOME': str(home),
			'USER': 'roboseasy', 'LANG': 'C.UTF-8'}

	def run(self, argv: list[str], **env: str) -> subprocess.CompletedProcess:
		"""가짜 PATH와 최소 환경(+ env)으로 실행한다. 이 PC의 환경변수는 넘기지 않는다."""
		return subprocess.run(argv, env={**self.env, **env}, capture_output=True, text=True, timeout=60,
			cwd=self.bin.parent)

	def start(self, argv: list[str], **env: str) -> subprocess.Popen:
		"""run과 같지만 기다리지 않는다. 새 세션이라 os.killpg로 프로세스 묶음 전체에 신호를 보낼 수 있다."""
		return subprocess.Popen(argv, env={**self.env, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
			text=True, cwd=self.bin.parent, start_new_session=True)

	def calls(self) -> list[str]:
		"""stub이 남긴 호출 기록 (한 줄에 하나)."""
		return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def fake_bin(tmp_path: Path) -> FakeBin:
	return FakeBin(tmp_path)


def tree_state(root: Path, skip: tuple[str, ...] = ()) -> dict[str, tuple]:
	"""root 아래 모든 경로의 종류 · 권한 · 내용(링크는 대상). 링크를 따라가지 않는다.

	Args:
		root: 가짜 루트.
		skip: 이 문자열로 시작하는 상대 경로는 뺀다 (예: 'etc/NetworkManager').
	"""
	state: dict[str, tuple] = {}
	for folder, dirs, files in os.walk(root):
		for name in dirs + files:
			path = Path(folder) / name
			rel = path.relative_to(root).as_posix()
			if rel.startswith(skip):
				continue
			mode = path.lstat().st_mode & 0o7777
			if path.is_symlink():
				state[rel] = ('link', os.readlink(path))
			elif path.is_dir():
				state[rel] = ('dir', mode)
			else:
				state[rel] = ('file', mode, hashlib.sha256(path.read_bytes()).hexdigest())
	return state


def nm_kf(fake: FakeBin, kf_dir: Path, *args: str) -> subprocess.CompletedProcess:
	return fake.run([str(ROBOT_NET / 'nm_kf.py'), *args], NM_KF_DIR=str(kf_dir))


def test_fake_bin_hides_real_system_commands(fake_bin: FakeBin) -> None:
	"""안전장치: stub 없는 시스템 명령은 이 PATH에서 보이지 않고, nmcli · sudo는 stub이다."""
	for command in ('systemctl', 'netplan', 'apt-get', 'iw', 'hostnamectl', 'reboot', 'shutdown', 'ssh', 'scp',
		'rsync', 'curl', 'systemd-run', 'shred', 'dd'):
		found = fake_bin.run(['bash', '-c', f'command -v {command}'])
		assert found.returncode != 0, f'{command}: {found.stdout}'
	missing = fake_bin.run(['bash', '-c', 'systemctl restart NetworkManager'])
	assert missing.returncode == 127 and 'command not found' in missing.stderr
	for command in ('nmcli', 'sudo', 'ip', 'pkill'):
		found = fake_bin.run(['bash', '-c', f'command -v {command}'])
		assert found.stdout.strip() == str(fake_bin.bin / command)


def test_nm_kf_set_then_get(fake_bin: FakeBin, tmp_path: Path) -> None:
	kf_dir = tmp_path / 'kf'
	kf_dir.mkdir()
	done = nm_kf(fake_bin, kf_dir, 'set', 'lekiwi-ap', 'wifi', 'ssid', 'lekiwi 04', 'wifi-security', 'psk',
		'pass word 12')
	assert done.returncode == 0, done.stderr
	assert nm_kf(fake_bin, kf_dir, 'get', 'lekiwi-ap', 'wifi', 'ssid').stdout == 'lekiwi 04\n'
	assert nm_kf(fake_bin, kf_dir, 'get', 'lekiwi-ap', 'wifi-security', 'psk').stdout == 'pass word 12\n'
	assert (kf_dir / 'lekiwi-ap.nmconnection').stat().st_mode & 0o777 == 0o600


def test_nm_kf_set_doubles_backslash(fake_bin: FakeBin, tmp_path: Path) -> None:
	kf_dir = tmp_path / 'kf'
	kf_dir.mkdir()
	assert nm_kf(fake_bin, kf_dir, 'set', 'home-wifi', 'wifi', 'ssid', 'a\\b').returncode == 0
	assert 'ssid=a\\\\b\n' in (kf_dir / 'home-wifi.nmconnection').read_text()


def test_nm_kf_del_and_missing(fake_bin: FakeBin, tmp_path: Path) -> None:
	kf_dir = tmp_path / 'kf'
	kf_dir.mkdir()
	nm_kf(fake_bin, kf_dir, 'set', 'home-wifi', 'ipv4', 'method', 'manual', 'ipv4', 'address1', '192.168.0.204/24')
	assert nm_kf(fake_bin, kf_dir, 'del', 'home-wifi', 'ipv4', 'address1').returncode == 0
	gone = nm_kf(fake_bin, kf_dir, 'get', 'home-wifi', 'ipv4', 'address1')
	assert (gone.returncode, gone.stdout) == (0, '\n')
	assert nm_kf(fake_bin, kf_dir, 'get', 'home-wifi', 'ipv4', 'method').stdout == 'manual\n'
	never = nm_kf(fake_bin, kf_dir, 'get', 'home-wifi', 'ipv4', 'dns')
	assert (never.returncode, never.stdout) == (0, '\n')


def test_nm_kf_keeps_other_sections(fake_bin: FakeBin, tmp_path: Path) -> None:
	kf_dir = tmp_path / 'kf'
	kf_dir.mkdir()
	(kf_dir / 'lekiwi-ap.nmconnection').write_text('[connection]\nid=lekiwi-ap\nautoconnect-priority=0\n\n'
		'[wifi]\nssid=old\n\n[ipv4]\nmethod=shared\naddress1=192.168.4.1/24\n')
	assert nm_kf(fake_bin, kf_dir, 'set', 'lekiwi-ap', 'wifi', 'ssid', 'new').returncode == 0
	assert (kf_dir / 'lekiwi-ap.nmconnection').read_text() == ('[connection]\nid=lekiwi-ap\n'
		'autoconnect-priority=0\n\n[wifi]\nssid=new\n\n[ipv4]\nmethod=shared\naddress1=192.168.4.1/24\n\n')


# 프로비저닝이 만드는 모양 그대로 (구현계획서 작업 3의 틀, 값은 가짜)
AP_KEYFILE = '''[connection]
id=lekiwi-ap
uuid=00000000-0000-4000-8000-000000000001
type=wifi
interface-name=wlan0
autoconnect=true
autoconnect-priority=0

[wifi]
mode=ap
ssid=lekiwi04
band=bg
channel=6

[wifi-security]
key-mgmt=wpa-psk
proto=rsn;
pairwise=ccmp;
group=ccmp;
psk=FAKEAPPSK-1

[ipv4]
method=shared
address1=192.168.4.1/24

[ipv6]
method=disabled
'''

HOME_KEYFILE = '''[connection]
id=home-wifi
uuid=00000000-0000-4000-8000-000000000002
type=wifi
interface-name=wlan0
autoconnect=true
autoconnect-priority=10

[wifi]
mode=infrastructure
ssid=roboseasy

[wifi-security]
key-mgmt=wpa-psk
psk=FAKEPSK-12345

[ipv4]
method=manual
address1=192.168.0.204/24,192.168.0.1
dns=8.8.8.8;1.1.1.1;

[ipv6]
method=disabled
'''


class Robot:
	"""keyfile 두 개(`lekiwi-ap` · `home-wifi`)가 든 가짜 폴더에서 robot_net 스크립트를 돌린다."""

	def __init__(self, fake: FakeBin, base: Path) -> None:
		self.fake = fake
		self.base = base
		self.kf_dir = base / 'system-connections'
		self.kf_dir.mkdir()
		self.state_dir = base / 'lekiwi-net'
		for name, text in (('lekiwi-ap', AP_KEYFILE), ('home-wifi', HOME_KEYFILE)):
			path = self.kf_dir / f'{name}.nmconnection'
			path.write_text(text)
			path.chmod(0o600)

	def run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess:
		return self.fake.run([str(ROBOT_NET / script), *args], NM_KF_DIR=str(self.kf_dir),
			LEKIWI_NET_STATE_DIR=str(self.state_dir), **env)

	def keyfile(self, name: str) -> str:
		return (self.kf_dir / f'{name}.nmconnection').read_text()

	def get(self, name: str, section: str, key: str) -> str | None:
		parser = configparser.RawConfigParser(delimiters=('=',), interpolation=None)
		parser.optionxform = str
		parser.read(self.kf_dir / f'{name}.nmconnection')
		return parser.get(section, key, fallback=None)

	def nmcli(self) -> list[str]:
		"""nmcli stub이 받은 인자만 (sudo 줄은 뺀다)."""
		return [line.removeprefix('nmcli ') for line in self.fake.calls() if line.startswith('nmcli ')]


@pytest.fixture
def robot(fake_bin: FakeBin, tmp_path: Path) -> Robot:
	return Robot(fake_bin, tmp_path)


def index_of(lines: list[str], text: str) -> int:
	"""text가 들어 있는 첫 줄의 번호. 없으면 시험 실패."""
	return next(i for i, line in enumerate(lines) if text in line)


@pytest.mark.parametrize('args', [
	('lekiwi07', 'abc1234'),                 # 비밀번호 7자
	('lekiwi07', 'a' * 64),                  # 64자
	('lekiwi07', 'abcd\n1234'),              # 줄바꿈
	('lekiwi\n07', 'abcd1234'),
	('lekiwi07', 'abcd1234\r'),
	('가' * 11, 'abcd1234'),                  # 33 바이트
	('', 'abcd1234'),                        # 빈 이름
])
def test_set_ap_rejects_bad_input(robot: Robot, args: tuple[str, str]) -> None:
	before = robot.keyfile('lekiwi-ap')
	done = robot.run('set_ap.sh', *args)
	assert done.returncode == 1
	assert robot.keyfile('lekiwi-ap') == before
	assert not [line for line in robot.nmcli() if 'con load' in line]


def test_set_ap_writes_keyfile_and_loads(robot: Robot) -> None:
	done = robot.run('set_ap.sh', '로봇 04', 'p@ss w&rd$1')
	assert done.returncode == 0, done.stdout + done.stderr
	assert (robot.get('lekiwi-ap', 'wifi', 'ssid'), robot.get('lekiwi-ap', 'wifi-security', 'psk')) == (
		'로봇 04', 'p@ss w&rd$1')
	assert f'con load {robot.kf_dir}/lekiwi-ap.nmconnection' in robot.nmcli()
	assert not [line for line in robot.nmcli() if 'modify' in line]
	assert 'con up lekiwi-ap' not in robot.nmcli()              # AP가 꺼져 있으면 다시 올리지 않는다
	assert robot.run('set_ap.sh', 'lekiwi04', 'abcd1234', FAKE_ACTIVE='lekiwi-ap').returncode == 0
	assert 'con up lekiwi-ap' in robot.nmcli()


def test_set_ap_without_args_prints_ssid_only(robot: Robot) -> None:
	done = robot.run('set_ap.sh')
	assert done.returncode == 0
	assert 'lekiwi04' in done.stdout
	assert 'FAKEAPPSK' not in done.stdout + done.stderr


def test_run_ap_disables_home_autoconnect_then_ap_up(robot: Robot) -> None:
	done = robot.run('run_ap.sh')
	assert done.returncode == 0, done.stdout + done.stderr
	assert robot.get('home-wifi', 'connection', 'autoconnect') == 'false'
	calls = robot.nmcli()
	assert index_of(calls, f'con load {robot.kf_dir}/home-wifi.nmconnection') < index_of(calls, 'con up lekiwi-ap')


def test_run_ap_failure_restores_router_autoconnect(robot: Robot) -> None:
	done = robot.run('run_ap.sh', FAKE_AP_UP_FAILS='1')
	assert done.returncode == 1
	assert 'error:' in done.stdout
	assert robot.get('home-wifi', 'connection', 'autoconnect') == 'true'
	calls = robot.nmcli()
	assert index_of(calls, 'con up lekiwi-ap') < index_of(calls, 'con up home-wifi')
	assert index_of(calls, 'con up lekiwi-ap') < max(i for i, line in enumerate(calls)
		if line == f'con load {robot.kf_dir}/home-wifi.nmconnection')


def test_run_wifi_saves_credentials_as_dhcp(robot: Robot) -> None:
	done = robot.run('run_wifi.sh', '로보시지 5G; $(x)', 'p@ss w&rd$1`')
	assert done.returncode == 0, done.stdout + done.stderr
	assert robot.get('home-wifi', 'wifi', 'ssid') == '로보시지 5G; $(x)'
	assert robot.get('home-wifi', 'wifi-security', 'psk') == 'p@ss w&rd$1`'
	assert robot.get('home-wifi', 'ipv4', 'method') == 'auto'
	assert robot.get('home-wifi', 'ipv4', 'address1') is None
	assert robot.get('home-wifi', 'ipv4', 'dns') is None
	assert robot.get('home-wifi', 'connection', 'autoconnect') == 'true'


def remove_router_profile(robot: Robot) -> None:
	"""이미지 직후 카드의 모양: 이미지 도구가 공유기 프로필(type=wifi, mode가 ap가 아닌 것)을 지운다."""
	(robot.kf_dir / 'home-wifi.nmconnection').unlink()


def test_run_wifi_creates_complete_profile_on_fresh_card(robot: Robot) -> None:
	remove_router_profile(robot)
	done = robot.run('run_wifi.sh', 'MyRouter', 'FAKEPSK-777')
	assert done.returncode == 0, done.stdout + done.stderr
	expected = {
		('connection', 'id'): 'home-wifi', ('connection', 'type'): 'wifi', ('connection', 'interface-name'): 'wlan0',
		('connection', 'autoconnect'): 'true', ('connection', 'autoconnect-priority'): '10',
		('wifi', 'mode'): 'infrastructure', ('wifi', 'ssid'): 'MyRouter',
		('wifi-security', 'key-mgmt'): 'wpa-psk', ('wifi-security', 'psk'): 'FAKEPSK-777',
		('ipv4', 'method'): 'auto', ('ipv6', 'method'): 'disabled',
	}
	for (section, key), value in expected.items():
		assert robot.get('home-wifi', section, key) == value, f'[{section}] {key}'
	uuid.UUID(robot.get('home-wifi', 'connection', 'uuid') or '')       # uuid 모양이 아니면 ValueError
	assert (robot.kf_dir / 'home-wifi.nmconnection').stat().st_mode & 0o777 == 0o600


def test_run_wifi_fresh_profile_is_accepted_by_libnm(robot: Robot) -> None:
	"""NetworkManager와 같은 라이브러리(libnm)가 읽고 검증한다. key-mgmt가 없는 프로필은 거절된다 (대조)."""
	gi = pytest.importorskip('gi')
	try:
		gi.require_version('NM', '1.0')
	except ValueError:
		pytest.skip('libnm(typelib)가 이 PC에 없다')
	from gi.repository import NM, GLib

	def load(text: str):
		keyfile = GLib.KeyFile()
		keyfile.load_from_data(text, len(text), GLib.KeyFileFlags.NONE)
		return NM.keyfile_read(keyfile, str(robot.kf_dir), NM.KeyfileHandlerFlags.NONE, None, None)

	remove_router_profile(robot)
	assert robot.run('run_wifi.sh', 'MyRouter', 'FAKEPSK-777').returncode == 0
	text = robot.keyfile('home-wifi')
	connection = load(text)
	assert connection.verify()
	assert connection.verify_secrets()                         # psk가 WPA 규칙(8~63자)에 맞는다
	with pytest.raises(GLib.Error, match='key-mgmt'):
		load(text.replace('key-mgmt=wpa-psk\n', '')).verify()


def test_run_wifi_keeps_existing_profile_identity(robot: Robot) -> None:
	uuid_before = robot.get('home-wifi', 'connection', 'uuid')
	assert robot.run('run_wifi.sh', 'NewRouter', 'FAKEPSK-888').returncode == 0
	assert robot.get('home-wifi', 'connection', 'uuid') == uuid_before
	assert robot.get('home-wifi', 'wifi', 'ssid') == 'NewRouter'


def test_run_wifi_after_run_ap_on_fresh_card_still_creates_complete_profile(robot: Robot) -> None:
	remove_router_profile(robot)
	assert robot.run('run_ap.sh').returncode == 0
	assert robot.run('run_wifi.sh', 'MyRouter', 'FAKEPSK-777').returncode == 0
	assert robot.get('home-wifi', 'connection', 'type') == 'wifi'
	assert robot.get('home-wifi', 'wifi-security', 'key-mgmt') == 'wpa-psk'


def test_run_ap_on_fresh_card_does_not_create_half_profile(robot: Robot) -> None:
	remove_router_profile(robot)
	assert robot.run('run_ap.sh').returncode == 0
	assert not (robot.kf_dir / 'home-wifi.nmconnection').exists()
	assert robot.run('run_ap.sh', FAKE_AP_UP_FAILS='1').returncode == 1
	assert not (robot.kf_dir / 'home-wifi.nmconnection').exists()


def test_run_wifi_without_saved_router_refuses_and_changes_nothing(robot: Robot) -> None:
	remove_router_profile(robot)
	done = robot.run('run_wifi.sh')
	assert done.returncode == 1
	assert [line for line in done.stdout.splitlines() if line.startswith('error:')]
	assert not (robot.kf_dir / 'home-wifi.nmconnection').exists()
	assert not [line for line in robot.nmcli() if 'con up' in line]


def test_run_wifi_failure_falls_back_to_ap(robot: Robot) -> None:
	done = robot.run('run_wifi.sh', FAKE_HOME_UP_FAILS='1')
	assert done.returncode == 1
	assert [line for line in done.stdout.splitlines() if line.startswith('error:')]
	assert 'con up lekiwi-ap' in robot.nmcli()


def test_set_ip_writes_fixed_address(robot: Robot) -> None:
	robot.run('run_wifi.sh', 'roboseasy', 'FAKEPSK-12345')       # DHCP로 바꿔 둔 뒤
	done = robot.run('set_ip.sh', '4', FAKE_ACTIVE='home-wifi')
	assert done.returncode == 0, done.stdout + done.stderr
	assert robot.get('home-wifi', 'ipv4', 'method') == 'manual'
	assert robot.get('home-wifi', 'ipv4', 'address1') == '192.168.0.204/24,192.168.0.1'
	assert robot.get('home-wifi', 'ipv4', 'dns') == '8.8.8.8;1.1.1.1;'
	assert not (robot.state_dir / 'home-wifi.prev').exists()


@pytest.mark.parametrize('kit', ['0', '55', 'a', ''])
def test_set_ip_rejects_out_of_range(robot: Robot, kit: str) -> None:
	before = robot.keyfile('home-wifi')
	assert robot.run('set_ip.sh', kit).returncode == 1
	assert robot.keyfile('home-wifi') == before


def test_apply_ipv4_failure_restores_previous_config(robot: Robot) -> None:
	before = robot.keyfile('home-wifi')
	done = robot.run('set_dhcp.sh', FAKE_ACTIVE='home-wifi', FAKE_HOME_UP_FAILS='1')
	assert done.returncode == 1
	assert 'error:' in done.stdout
	assert sum(line.endswith('con up home-wifi') for line in robot.nmcli()) == 2   # 적용 시도 → 되돌린 뒤 다시
	assert robot.keyfile('home-wifi') == before
	assert not (robot.state_dir / 'home-wifi.prev').exists()


def test_apply_ipv4_in_ap_mode_only_saves(robot: Robot) -> None:
	done = robot.run('set_dhcp.sh', FAKE_ACTIVE='lekiwi-ap')
	assert done.returncode == 0, done.stdout + done.stderr
	assert robot.get('home-wifi', 'ipv4', 'method') == 'auto'
	assert not [line for line in robot.nmcli() if line.endswith('con up home-wifi')]
	assert not (robot.state_dir / 'home-wifi.prev').exists()


def test_switch_scripts_only_touch_paths_inside_tmp(robot: Robot, tmp_path: Path) -> None:
	"""안전장치: 스크립트가 넘긴 모든 절대 경로가 임시 폴더나 robot_net 안이다 (이 PC의 /etc · /var가 아니다)."""
	robot.run('run_wifi.sh', 'roboseasy', 'FAKEPSK-12345')
	robot.run('set_ip.sh', '4', FAKE_ACTIVE='home-wifi')
	robot.run('set_dhcp.sh', FAKE_ACTIVE='home-wifi', FAKE_HOME_UP_FAILS='1')
	robot.run('run_ap.sh', FAKE_AP_UP_FAILS='1')
	robot.run('set_ap.sh', 'lekiwi04', 'abcd1234', FAKE_ACTIVE='lekiwi-ap')
	paths = [word for line in robot.fake.calls() for word in line.split() if word.startswith('/')]
	assert paths
	outside = [path for path in paths if not path.startswith((str(tmp_path), str(ROBOT_NET)))]
	assert outside == []


def code_lines(path: Path) -> list[str]:
	"""주석 줄을 뺀 줄 (금지 낱말을 설명하는 주석은 괜찮다)."""
	return [line for line in path.read_text().splitlines() if not line.lstrip().startswith('#')]


def test_scripts_never_use_nmcli_modify() -> None:
	scripts = sorted(ROBOT_NET.glob('*.sh')) + sorted(ROBOT_NET.glob('*.py'))
	assert len(scripts) >= 8
	for script in scripts:
		for line in code_lines(script):
			assert 'con modify' not in line and 'connection modify' not in line, f'{script.name}: {line}'


def test_scripts_pass_bash_syntax_check() -> None:
	scripts = sorted(ROBOT_NET.glob('*.sh'))
	assert len(scripts) >= 7
	for script in scripts:
		checked = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
		assert checked.returncode == 0, f'{script.name}: {checked.stderr}'


def test_run_ap_failure_keeps_saved_ap_mode(robot: Robot) -> None:
	"""AP 고정 모드(자동 연결 false)에서 run_ap가 실패해도 공유기로 바꾸지 않는다."""
	nm_kf(robot.fake, robot.kf_dir, 'set', 'home-wifi', 'connection', 'autoconnect', 'false')
	done = robot.run('run_ap.sh', FAKE_AP_UP_FAILS='1')
	assert done.returncode == 1
	assert [line for line in done.stdout.splitlines() if line.startswith('error:')]
	assert robot.get('home-wifi', 'connection', 'autoconnect') == 'false'
	assert not [line for line in robot.nmcli() if 'con up home-wifi' in line]


def test_run_ap_failure_restores_missing_autoconnect_key(robot: Robot) -> None:
	"""키가 없던 프로필(기본값 true)은 키 없이 되돌리고 공유기를 다시 올린다."""
	nm_kf(robot.fake, robot.kf_dir, 'del', 'home-wifi', 'connection', 'autoconnect')
	assert robot.run('run_ap.sh', FAKE_AP_UP_FAILS='1').returncode == 1
	assert robot.get('home-wifi', 'connection', 'autoconnect') is None
	assert index_of(robot.nmcli(), 'con up lekiwi-ap') < index_of(robot.nmcli(), 'con up home-wifi')


@pytest.mark.parametrize('args', [(' roboseasy', 'abcd1234'), ('roboseasy ', 'abcd1234'), ('roboseasy', ' abcd1234'),
	('roboseasy', 'abcd1234 ')], ids=['ssid-head', 'ssid-tail', 'psk-head', 'psk-tail'])
def test_run_wifi_rejects_edge_spaces(robot: Robot, args: tuple[str, str]) -> None:
	"""keyfile은 값의 앞뒤 공백을 잘라 읽는다: 저장하면 다른 이름 · 비밀번호가 된다."""
	before = robot.keyfile('home-wifi')
	assert robot.run('run_wifi.sh', *args).returncode == 1
	assert robot.keyfile('home-wifi') == before
	assert not robot.nmcli()
