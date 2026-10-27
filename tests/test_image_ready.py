"""image_ready_check.sh 시험: 이미지 직전 점검 (읽기 전용, 비밀번호 값은 출력하지 않는다).

임시 CHECK_ROOT에 골든 카드 모양 트리를 만들고 가짜 PATH(test_robot_net의 FakeBin, `nmcli` · `df` · `du`는 stub)에서
돌린다.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from test_provision import NETPLAN, NETPLAN_WITHOUT_WIFIS
from test_robot_net import AP_KEYFILE, HOME_KEYFILE, ROBOT_NET, FakeBin, tree_state

CHECK = ROBOT_NET / 'image_ready_check.sh'
HOME = '/home/roboseasy'
H = HOME[1:]

CHECK_STUBS: dict[str, str] = {
	'nmcli': 'echo "nmcli $*" >> "$FAKE_LOG"\nprintf "lekiwi-ap:802-11-wireless\\n"\n',
	'df': '''echo "df $*" >> "$FAKE_LOG"
printf "Filesystem      Size  Used Avail Use%% Mounted on\\n/dev/mmcblk0p2   29G   12G   16G  43%% /\\n"
''',
	'du': '''echo "du $*" >> "$FAKE_LOG"
for p in "$@"; do case $p in -*) ;; *) printf "1\\t%s\\n" "$p" ;; esac; done
''',
}

# 이미지 도구가 쓰는 첫 부팅 설정의 기본 꼴: 주석 처리한 예시 줄은 문제가 아니다
CLEAN_NETWORK_CONFIG = '''network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
      optional: true
#  wifis:
#    wlan0:
#      access-points:
#        myhomewifi:
#          password: "S3kr1t"
'''


AP_PSK = 'FAKEPSK-AP-99'          # 고객이 바꾼 AP 비밀번호 (공유기 값 FAKEPSK-12345의 일부가 아니어야 시험이 섞이지 않는다)
# 처음 AP: 모든 로봇이 이름 · 비밀번호 lekiwi01 (사용자 결정, 공개된 값)
DEFAULT_AP = AP_KEYFILE.replace('ssid=lekiwi04', 'ssid=lekiwi01').replace('psk=FAKEAPPSK-1', 'psk=lekiwi01')


class CheckTree:
	"""허용된 것만 있는 골든 카드 모양 트리. write로 문제를 하나씩 더한다."""

	def __init__(self, base: Path, extra_stubs: dict[str, str] | None = None) -> None:
		self.fake = FakeBin(base, {**CHECK_STUBS, **(extra_stubs or {})})
		self.root = base / 'root'
		self.write('etc/hostname', 'lekiwi04\n')
		self.write('etc/kit-number', '4\n')
		self.write('etc/passwd', 'root:x:0:0:root:/root:/bin/bash\nroboseasy:x:1000:1000:roboseasy,,,:/home/roboseasy:/bin/bash\n')
		self.write('etc/group', 'root:x:0:\nsudo:x:27:roboseasy\nroboseasy:x:1000:\n')
		self.write('etc/netplan/50-cloud-init.yaml', NETPLAN_WITHOUT_WIFIS, 0o600)
		self.write('boot/firmware/network-config', CLEAN_NETWORK_CONFIG)
		self.write('etc/NetworkManager/system-connections/lekiwi-ap.nmconnection',
			DEFAULT_AP, 0o600)
		self.write('etc/NetworkManager/conf.d/default-wifi-powersave-on.conf', '[connection]\nwifi.powersave = 2\n')
		self.write('var/lib/dpkg/status', 'Package: bash\nStatus: install ok installed\n')
		self.write('var/backups/dpkg.status.0', 'Package: bash\n')
		self.write('var/log/syslog', 'small\n')
		self.write(f'{H}/.bashrc', '# bashrc\n')
		self.write(f'{H}/.ssh/authorized_keys', 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPLX physical-labs-lekiwi\n', 0o600)
		self.write(f'{H}/.ssh/id_ed25519.pub', 'ssh-ed25519 AAAA test\n')
		for script in ('run_wifi.sh', 'set_ap.sh', 'nm_kf.py', 'clone_init.sh', 'wifi_list.sh'):
			if (ROBOT_NET / script).exists():
				self.write(f'{H}/lekiwi_net/{script}', (ROBOT_NET / script).read_text(), 0o755)
		self.write(f'{H}/lekiwi_net/provision_ap_mode.sh',      # psk= 줄이 든 틀 (이 저장소째 로봇에 실린다)
			(ROBOT_NET / 'provision_ap_mode.sh').read_text(), 0o755)
		self.write(f'{H}/lekiwi_ws/build/lekiwi_motor_driver/lekiwi_motor_driver_node', 'elf\n', 0o755)
		link = self.root / f'{H}/lekiwi_ws/install/lekiwi_motor_driver/lib/lekiwi_motor_driver_node'
		link.parent.mkdir(parents=True)
		link.symlink_to(self.root / f'{H}/lekiwi_ws/build/lekiwi_motor_driver/lekiwi_motor_driver_node')
		(self.root / 'run').mkdir()
		wants = self.root / 'etc/systemd/system/multi-user.target.wants'
		wants.mkdir(parents=True)
		(wants / 'NetworkManager.service').symlink_to('/usr/lib/systemd/system/NetworkManager.service')
		self.write('dev/mmcblk0p1', 'FAT32   config.txt cmdline.txt network-config\n' * 20)   # 부트 파티션의 원시 장치 흉내

	def write(self, rel: str, text: str = 'x\n', mode: int = 0o644) -> Path:
		path = self.root / rel
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text)
		path.chmod(mode)
		return path

	def check(self) -> subprocess.CompletedProcess:
		return self.fake.run([str(CHECK)], CHECK_ROOT=str(self.root), CHECK_HOME=HOME,
			CHECK_BOOT_DEVICE=str(self.root / 'dev/mmcblk0p1'))


@pytest.fixture
def tree(tmp_path: Path) -> CheckTree:
	return CheckTree(tmp_path)


def lines(done: subprocess.CompletedProcess, level: str) -> list[str]:
	return [line for line in done.stdout.splitlines() if line.startswith(f'[{level}]')]


def assert_flagged(done: subprocess.CompletedProcess, level: str, needle: str) -> None:
	found = [line for line in lines(done, level) if needle in line]
	assert found, f'{level} {needle!r} 없음:\n{done.stdout}{done.stderr}'


def test_clean_tree_passes(tree: CheckTree) -> None:
	done = tree.check()
	assert done.returncode == 0, done.stdout + done.stderr
	assert lines(done, 'FAIL') == [] and lines(done, 'WARN') == []
	assert done.stdout.rstrip().splitlines()[-1] == '요약: FAIL 0 · WARN 0'
	assert lines(done, 'OK')
	assert 'wifi_list.sh' not in done.stdout
	assert [line for line in lines(done, 'INFO') if 'lekiwi04' in line]       # 호스트 이름 · AP 이름


@pytest.mark.parametrize('rel, needle', [
	(f'{H}/backups/x.tgz', f'{HOME}/backups'),
	('var/backups/lekiwi_net_1.tar.gz', '/var/backups/lekiwi_net_1.tar.gz'),
	(f'{H}/robot_net_staging/provision.log', f'{HOME}/robot_net_staging'),
	('var/lib/lekiwi-net/home-wifi.prev', '/var/lib/lekiwi-net'),
	('run/lekiwi-ap-password', '/run/lekiwi-ap-password'),
	(f'{H}/.bashrc.bak-20261017', f'{HOME}/.bashrc.bak-20261017'),
	(f'{H}/lekiwi_ws/robot_profile.yaml.bak-20261022-latency', 'robot_profile.yaml.bak-20261022-latency'),
])
def test_backups_and_work_traces_fail(tree: CheckTree, rel: str, needle: str) -> None:
	tree.write(rel)
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', needle)


def test_password_files_fail_except_ap_keyfile(tree: CheckTree) -> None:
	tree.write('etc/NetworkManager/system-connections/home-wifi.nmconnection', HOME_KEYFILE, 0o600)
	done = tree.check()
	assert done.returncode == 0, done.stdout
	assert lines(done, 'FAIL') == []
	assert_flagged(done, 'WARN', 'home-wifi.nmconnection')
	assert 'lekiwi-ap.nmconnection' not in ''.join(lines(done, 'FAIL') + lines(done, 'WARN'))


@pytest.mark.parametrize('rel, text', [
	(f'{H}/old.bak', '          password: "FAKEPSK-2"\n'),
	(f'{H}/notes.txt', 'psk=FAKEPSK-2\n'),                      # 이름이 아니라 내용으로 잡는다
	('etc/netplan/.50-cloud-init.yaml.bak', NETPLAN),
	('etc/netplan/50-cloud-init.yaml', NETPLAN),
	('boot/firmware/network-config', NETPLAN),
	('var/lib/cloud/x.conf', 'wpa-psk FAKEPSK-2\n'),
	('etc/NetworkManager/system-connections/cafe.nmconnection', 'psk=FAKEPSK-2\n'),
])
def test_password_files_and_wifis_fail(tree: CheckTree, rel: str, text: str) -> None:
	tree.write(rel, text)
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', '/' + rel)


@pytest.mark.parametrize('rel', [
	f'{H}/set_wifi.sh', f'{H}/set_wifi.sh.legacy-netplan', 'etc/networkd-dispatcher/routable.d/50-kit-ip',
	'etc/systemd/system/lekiwi_host.service', f'{H}/clone_init.sh', f'{H}/wifi_list.sh',
])
def test_legacy_tools_fail(tree: CheckTree, rel: str) -> None:
	tree.write(rel)
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', '/' + rel)
	assert 'lekiwi_net/wifi_list.sh' not in done.stdout          # 옮겨 간 새 자리는 문제가 아니다


def test_wifi_list_command_needs_installed_script_and_new_bashrc_path(tree: CheckTree) -> None:
	"""홈의 옛 파일만 지워 FAIL을 넘기면 `wifi list`가 깨진다: 새 자리의 파일과 .bashrc가 부르는 경로도 본다."""
	assert tree.check().returncode == 0
	(tree.root / f'{H}/lekiwi_net/wifi_list.sh').unlink()
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', f'{HOME}/lekiwi_net/wifi_list.sh')
	tree.write(f'{H}/lekiwi_net/wifi_list.sh', '#!/bin/bash\n', 0o644)     # 실행 권한이 없어도 FAIL
	assert_flagged(tree.check(), 'FAIL', f'{HOME}/lekiwi_net/wifi_list.sh')
	tree.write(f'{H}/lekiwi_net/wifi_list.sh', '#!/bin/bash\n', 0o755)
	tree.write(f'{H}/.bashrc', 'wifi() { "$HOME/wifi_list.sh" "${2:-}"; }\n')
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', f'{HOME}/.bashrc')


@pytest.mark.parametrize('rel, needle', [
	(f'{H}/.vscode-server/bin/x', f'{HOME}/.vscode-server'),
	(f'{H}/lekiwi_web_operator/.omc/state.json', f'{HOME}/lekiwi_web_operator/.omc'),
	(f'{H}/lekiwi_ws/src/pkg/.omc/x', f'{HOME}/lekiwi_ws/src/pkg/.omc'),
	(f'{H}/.claude/settings.json', f'{HOME}/.claude'),
	(f'{H}/.claude.json', f'{HOME}/.claude.json'),
	(f'{H}/.ssh/id_ed25519', f'{HOME}/.ssh/id_ed25519'),
])
def test_dev_tool_state_fails(tree: CheckTree, rel: str, needle: str) -> None:
	tree.write(rel)
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', needle)


def test_broken_install_symlink_fails(tree: CheckTree) -> None:
	assert tree.check().returncode == 0
	(tree.root / f'{H}/lekiwi_ws/build/lekiwi_motor_driver/lekiwi_motor_driver_node').unlink()
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', 'lekiwi_ws/install/lekiwi_motor_driver/lib/lekiwi_motor_driver_node')


@pytest.mark.parametrize('rel, text, needle', [
	(f'{H}/lekiwi_ws/robot_profile.yaml', 'x\n', 'robot_profile.yaml'),
	(f'{H}/lekiwi_ws/arm_profile.yaml', 'x\n', 'arm_profile.yaml'),
	(f'{H}/lekiwi_ws/arm_calibration_restore.json', '{}\n', 'arm_calibration_restore.json'),
	(f'{H}/.cache/huggingface/lerobot/calibration/robots/lekiwi/lekiwi04.json', '{}\n', 'lekiwi04.json'),
	(f'{H}/maps/lekiwi/map.pgm', 'P5\n', f'{HOME}/maps'),
	('var/log/syslog', 'x' * (1536 * 1024), '/var/log/syslog'),
	('var/log/journal/abc/system.journal', 'x\n', '/var/log/journal'),
	(f'{H}/.ros/log/x.log', 'x\n', '.ros/log'),
	(f'{H}/lekiwi_ws/log/build.log', 'x\n', 'lekiwi_ws/log'),
	('var/lib/apt/lists/ports.ubuntu.com_dists_noble_main_binary-arm64_Packages', 'Package: x\n', '/var/lib/apt/lists'),
	('var/cache/apt/archives/x_1.0_arm64.deb', 'x\n', '/var/cache/apt/archives'),
	('var/lib/NetworkManager/dnsmasq-wlan0.leases', 'x\n', 'dnsmasq-wlan0.leases'),
	('var/lib/NetworkManager/secret_key', 'x\n', 'secret_key'),
	('var/lib/NetworkManager/internal-x-wlan0.lease', 'x\n', 'internal-x-wlan0.lease'),
	('var/lib/NetworkManager/seen-bssids', '[seen-bssids]\n', 'seen-bssids'),
	('var/lib/NetworkManager/timestamps', '[timestamps]\n', 'timestamps'),
	('etc/hostname', 'roboseasy-pi02\n', 'roboseasy-pi02'),       # lekiwiNN 꼴이 아니다
])
def test_warns_for_robot_specific_values_and_logs(tree: CheckTree, rel: str, text: str, needle: str) -> None:
	tree.write(rel, text)
	done = tree.check()
	assert done.returncode == 0, done.stdout
	assert lines(done, 'FAIL') == []
	assert_flagged(done, 'WARN', needle)


def test_script_folders_are_not_flagged(tree: CheckTree) -> None:
	tree.write(f'{H}/lekiwi_net/x.sh', 'psk=$PSK\n')
	tree.write(f'{H}/lekiwi_web_operator/robot_net/y.sh', 'psk=$PSK\npassword: "$PW"\n')
	done = tree.check()
	assert done.returncode == 0, done.stdout
	assert lines(done, 'FAIL') == []


def test_repository_tests_folder_is_not_flagged(tree: CheckTree) -> None:
	"""~/lekiwi_net 이 저장소째 (git clone) 이면 tests/ 의 가짜 비밀번호 · 가짜 키는 이미지 문제가 아니다."""
	tree.write(f'{H}/lekiwi_net/tests/test_x.py', 'psk=FAKEPSK-12345\n-----BEGIN PRIVATE KEY-----\nMIIE\n')
	done = tree.check()
	assert done.returncode == 0, done.stdout
	assert lines(done, 'FAIL') == []


def dirty(tree: CheckTree) -> None:
	"""비밀번호가 여러 곳에 남은 트리."""
	tree.write('etc/NetworkManager/system-connections/home-wifi.nmconnection', HOME_KEYFILE, 0o600)
	tree.write(f'{H}/old.bak', 'password: "FAKEPSK-2"\n')
	tree.write('etc/netplan/.50-cloud-init.yaml.bak', NETPLAN.replace('FAKEPSK-12345', 'FAKEPSK-3'))
	tree.write('var/lib/lekiwi-net/home-wifi.prev', HOME_KEYFILE.replace('FAKEPSK-12345', 'FAKEPSK-4'), 0o600)
	tree.write('run/lekiwi-ap-password', 'FAKEPSK-5\n', 0o600)
	tree.write(f'{H}/backups/x.tgz')


def test_never_prints_secret_values(tree: CheckTree) -> None:
	dirty(tree)
	done = tree.check()
	assert done.returncode == 1
	assert 'FAKEPSK' not in done.stdout + done.stderr
	assert len(lines(done, 'FAIL')) >= 5


def test_check_is_read_only(tree: CheckTree) -> None:
	dirty(tree)
	before = tree_state(tree.root)
	tree.check()
	assert tree_state(tree.root) == before
	assert {line.split()[0] for line in tree.fake.calls()} <= {'nmcli', 'df', 'du'}
	assert [line for line in tree.fake.calls() if line.startswith('nmcli')] == ['nmcli -t -f NAME,TYPE con show']


def set_ap(tree: CheckTree, name: str = 'lekiwi01', password: str = AP_PSK) -> None:
	"""lekiwi-ap 프로필의 이름 · 비밀번호를 바꾼다 (기본: 고객이 비밀번호만 바꾼 꼴)."""
	tree.write('etc/NetworkManager/system-connections/lekiwi-ap.nmconnection',
		DEFAULT_AP.replace('ssid=lekiwi01', f'ssid={name}').replace('psk=lekiwi01', f'psk={password}'), 0o600)


def plant_values(tree: CheckTree) -> None:
	"""패턴(psk= · password:)으로는 안 걸리는 곳에 실제 값을 남긴다 (AP 비밀번호는 고객이 바꾼 값)."""
	set_ap(tree)
	tree.write('etc/NetworkManager/system-connections/home-wifi.nmconnection', HOME_KEYFILE, 0o600)
	tree.write('var/lib/cloud/instances/x/network-config.json',
		'{"wifis": {"wlan0": {"access-points": {"roboseasy": {"password": "FAKEPSK-12345"}}}}}\n')
	tree.write('var/log/auth.log', 'sudo: roboseasy : COMMAND=/home/roboseasy/set_wifi.sh roboseasy FAKEPSK-12345\n')
	tree.write(f'{H}/.bash_history', f'sudo ~/lekiwi_net/set_ap.sh lekiwi04 {AP_PSK}\n')


def test_secret_value_found_in_cloud_init_json_and_logs_fails(tree: CheckTree) -> None:
	plant_values(tree)
	done = tree.check()
	assert done.returncode == 1
	router = [line for line in lines(done, 'FAIL') if '공유기 비밀번호 값' in line]
	assert_flagged(done, 'FAIL', '/var/lib/cloud/instances/x/network-config.json')   # JSON 줄 모양으로 먼저 잡힌다
	assert any('/var/log/auth.log' in line for line in router), done.stdout
	ap = [line for line in lines(done, 'FAIL') if 'AP 비밀번호 값' in line]
	assert ap == [f'[FAIL] AP 비밀번호 값이 든 파일: {HOME}/.bash_history'], done.stdout
	assert not [line for line in router + ap if 'home-wifi.nmconnection' in line or 'lekiwi-ap.nmconnection' in line]


def test_secret_value_only_in_profiles_is_ok(tree: CheckTree) -> None:
	set_ap(tree)
	tree.write('etc/NetworkManager/system-connections/home-wifi.nmconnection', HOME_KEYFILE, 0o600)
	tree.write('etc/netplan/.50-cloud-init.yaml.bak2', 'x\n')         # netplan 폴더의 다른 파일은 값이 없다
	done = tree.check()
	assert done.returncode == 0, done.stdout
	assert lines(done, 'FAIL') == []
	assert_flagged(done, 'WARN', 'home-wifi.nmconnection')
	assert_flagged(done, 'OK', '공유기 비밀번호 값')
	assert_flagged(done, 'OK', 'AP 비밀번호 값')


def test_secret_tracing_skipped_without_profiles(tree: CheckTree) -> None:
	(tree.root / 'etc/NetworkManager/system-connections/lekiwi-ap.nmconnection').unlink()
	done = tree.check()
	assert_flagged(done, 'INFO', '값 추적: 건너뜀')
	assert not [line for line in done.stdout.splitlines() if '비밀번호 값' in line]


def test_secret_tracing_never_prints_value(tree: CheckTree) -> None:
	plant_values(tree)
	dirty(tree)
	done = tree.check()
	assert done.returncode == 1
	assert 'FAKEPSK' not in done.stdout + done.stderr


def test_secret_values_never_in_command_lines(tmp_path: Path) -> None:
	"""값은 grep -f 의 프로세스 치환으로만 넘긴다: 다른 사용자가 ps로 볼 수 있는 명령줄에 없다."""
	wrapped = {tool: f'echo "{tool} $*" >> "$FAKE_LOG"\nexec {shutil.which(tool)} "$@"\n'
		for tool in ('grep', 'sed', 'head', 'awk', 'cat', 'find', 'tr', 'sort', 'cut', 'python3')}
	tree = CheckTree(tmp_path, wrapped)
	plant_values(tree)
	done = tree.check()
	assert done.returncode == 1
	calls = tree.fake.calls()
	assert [line for line in calls if line.startswith('grep ') and ' -f ' in line]       # 값으로 찾았다
	assert not [line for line in calls if 'FAKEPSK' in line]


# --- 값 추적 씨앗 · 범위 (5a · 5b)

def test_secret_tracing_uses_wpa_hex_psk(tree: CheckTree) -> None:
	"""공유기 (SSID, 비밀번호)로 만든 64자 hex PSK도 찾는다 (wpa_supplicant 설정 · 로그에 이 꼴로 남는다)."""
	tree.write('etc/NetworkManager/system-connections/home-wifi.nmconnection', HOME_KEYFILE, 0o600)
	hexpsk = hashlib.pbkdf2_hmac('sha1', b'FAKEPSK-12345', b'roboseasy', 4096, 32).hex()
	tree.write('var/log/syslog', f'wpa_supplicant: using psk {hexpsk}\n')
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', '/var/log/syslog')
	assert hexpsk not in done.stdout + done.stderr


def test_secret_tracing_seeds_values_found_by_pattern(tree: CheckTree) -> None:
	"""패턴으로 찾은 값(프로필이 아닌 곳의 비밀번호)도 씨앗이 된다: 같은 값이 패턴 없는 로그에 있으면 잡는다."""
	tree.write(f'{H}/notes.txt', 'psk=FAKEPSK-77777\n')
	tree.write('var/log/auth.log', 'sudo: roboseasy : COMMAND=/usr/bin/nmcli dev wifi connect x FAKEPSK-77777\n')
	done = tree.check()
	assert_flagged(done, 'FAIL', f'{HOME}/notes.txt')
	assert_flagged(done, 'FAIL', '/var/log/auth.log')
	assert 'FAKEPSK' not in done.stdout + done.stderr


def test_secret_tracing_tells_why_router_values_are_missing(tree: CheckTree) -> None:
	done = tree.check()                                          # 깨끗한 트리: AP 값만 있다
	assert [line for line in lines(done, 'INFO') if '공유기' in line and '건너뜀' in line], done.stdout


@pytest.mark.parametrize('rel', ['var/lib/foo/state.json', 'opt/tool/settings.json'])
def test_json_password_fields_fail(tree: CheckTree, rel: str) -> None:
	tree.write(rel, '{"ssid": "cafe", "psk": "FAKEPSK-55555"}\n')
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', '/' + rel)


@pytest.mark.parametrize('rel', ['opt/tool/conf.yaml', 'usr/local/etc/x.yaml', 'srv/x.yaml', 'root/notes.yaml',
	'home/other/x.yaml', f'{H}/lekiwi_web_operator/logs/x.yaml', f'{H}/.cache/x/y.yaml', 'var/tmp/x.yaml'])
def test_wider_scan_finds_password_lines(tree: CheckTree, rel: str) -> None:
	tree.write(rel, 'password: "FAKEPSK-33333"\n')
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', '/' + rel)


@pytest.mark.parametrize('rel', [f'{H}/lerobot060_venv/lib/x.py', f'{H}/lekiwi_ws/build/x/y.txt',
	f'{H}/lekiwi_ws/install/x/z.txt', f'{H}/.vscode-server/data/x.log', 'opt/ros/jazzy/share/x.yaml',
	'var/lib/apt/lists/x_Packages'])
def test_big_code_and_package_folders_are_not_scanned(tree: CheckTree, rel: str) -> None:
	tree.write('etc/NetworkManager/system-connections/home-wifi.nmconnection', HOME_KEYFILE, 0o600)
	tree.write(rel, 'password: "FAKEPSK-12345"\n')
	done = tree.check()
	assert not [line for line in lines(done, 'FAIL') if rel.rsplit('/', 1)[-1] in line], done.stdout


@pytest.mark.parametrize('text', ['password: k.password.value,\n', '    password: Optional[str] = None\n',
	'psk=$APPW\n', 'password: "${PW}"\n', 'psk=<AP 비밀번호>\n', '    password: str\n'])
def test_code_and_placeholders_are_not_secrets(tree: CheckTree, text: str) -> None:
	tree.write(f'{H}/lekiwi_web_operator/web/js/x.js', text)
	done = tree.check()
	assert done.returncode == 0, done.stdout
	assert lines(done, 'FAIL') == []


# --- 값 없이 걸리는 흔적 · 토큰 · 상태 (5c · 5d · 5f)

@pytest.mark.parametrize('rel, text, needle', [
	('var/lib/cloud/instances/x/network-config.json', '{"wifis": {"wlan0": {}}}\n', 'network-config.json'),
	('var/log/syslog.2.gz', 'x', 'syslog.2.gz'),
	('var/log/journal/x/y.journal.zst', 'x', 'y.journal.zst'),
	('var/crash/_usr_bin_python3.1000.crash', 'x', '.crash'),
	('var/lib/systemd/coredump/core.x.lz4', 'x', 'core.x.lz4'),
	('var/log/auth.log', 'sudo: COMMAND=/home/roboseasy/lekiwi_net/run_wifi.sh roboseasy secret99\n', '/var/log/auth.log'),
	('var/log/syslog', 'gw: sudo -n /home/roboseasy/lekiwi_net/set_ap.sh lekiwi04 secret99\n', '/var/log/syslog'),
	('var/log/syslog', 'x: /home/roboseasy/lekiwi_net/nm_kf.py set lekiwi-ap wifi-security psk secret99\n', '/var/log/syslog'),
	('root/old.tgz', 'x', '/root/old.tgz'),
	('tmp/web.tar.gz', 'x', '/tmp/web.tar.gz'),
	('etc/wpa_supplicant/wpa_supplicant.conf', 'network={\n  ssid="cafe"\n  psk="FAKEPSK-44444"\n}\n',
		'wpa_supplicant.conf'),
])
def test_traces_without_known_values_fail(tree: CheckTree, rel: str, text: str, needle: str) -> None:
	tree.write(rel, text)
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', needle)
	assert 'secret99' not in done.stdout + done.stderr


def test_system_backups_are_not_archives(tree: CheckTree) -> None:
	tree.write('var/backups/alternatives.tar.0', 'x')
	tree.write('var/backups/dpkg.status.1.gz', 'x')
	assert tree.check().returncode == 0


@pytest.mark.parametrize('rel', [f'{H}/.cache/huggingface/token', f'{H}/.huggingface/token', f'{H}/.git-credentials',
	f'{H}/.netrc', f'{H}/.config/gh/hosts.yml', 'root/.git-credentials'])
def test_tokens_fail(tree: CheckTree, rel: str) -> None:
	tree.write(rel, 'hf_FAKETOKEN\n')
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', '/' + rel)
	assert 'FAKETOKEN' not in done.stdout


def test_private_keys_are_found_by_header(tree: CheckTree) -> None:
	key = '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n'
	tree.write(f'{H}/keys/github', key)
	tree.write('etc/ssh/ssh_host_ed25519_key', key, 0o600)         # 호스트키는 이미지 도구가 지우고 첫 부팅에 새로 만든다
	done = tree.check()
	assert_flagged(done, 'FAIL', f'{HOME}/keys/github')
	assert 'ssh_host_ed25519_key' not in done.stdout
	assert 'b3BlbnNzaC1rZXkt' not in done.stdout


@pytest.mark.parametrize('rel', ['boot/firmware/user-data', 'var/lib/cloud/instances/x/user-data.txt',
	'var/lib/cloud/instances/x/cloud-config.txt'])
def test_cloud_init_login_password_is_a_warning(tree: CheckTree, rel: str) -> None:
	"""로그인 설정의 password:는 공유기 비밀번호가 아니다: FAIL이 아니라 WARN, 값은 찍지 않는다."""
	tree.write(rel, '#cloud-config\nchpasswd:\n  expire: false\npassword: FAKELOGIN-1\n')
	done = tree.check()
	assert done.returncode == 0, done.stdout
	assert_flagged(done, 'WARN', '/' + rel)
	assert 'FAKELOGIN' not in done.stdout + done.stderr


def test_networkmanager_hold_and_disabled_fail(tree: CheckTree) -> None:
	tree.write('etc/NetworkManager/conf.d/90-lekiwi-hold.conf', '[keyfile]\nunmanaged-devices=*\n')
	(tree.root / 'etc/systemd/system/multi-user.target.wants/NetworkManager.service').unlink()
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', '90-lekiwi-hold.conf')
	assert_flagged(done, 'FAIL', 'NetworkManager.service')


@pytest.mark.parametrize('old, new', [('autoconnect=true\n', 'autoconnect=false\n'), ('band=bg\n', 'band=a\n')])
def test_ap_profile_settings_warn(tree: CheckTree, old: str, new: str) -> None:
	path = tree.root / 'etc/NetworkManager/system-connections/lekiwi-ap.nmconnection'
	path.write_text(path.read_text().replace(old, new, 1))
	done = tree.check()
	assert done.returncode == 0
	assert_flagged(done, 'WARN', new.strip())


def test_empty_state_dir_is_ok(tree: CheckTree) -> None:
	(tree.root / 'var/lib/lekiwi-net').mkdir(parents=True)
	assert tree.check().returncode == 0


def test_authorized_keys_lists_personal_key_comments(tree: CheckTree) -> None:
	tree.write(f'{H}/.ssh/authorized_keys', 'ssh-ed25519 AAAAC3NzaPHYS1 physical-labs-lekiwi\n'
		'ssh-ed25519 AAAAC3NzaKHW99 khw@roboseasy\n'
		'from="10.0.0.0/8" ssh-rsa AAAAB3NzaNAMIN namin@namin-Legion\n', 0o600)
	done = tree.check()
	assert done.returncode == 0
	warn = [line for line in lines(done, 'WARN') if 'authorized_keys' in line]
	assert warn and '키 3개 = 앱 설치 1 + 개인 2' in warn[0] and 'root로' in warn[0], done.stdout
	assert 'khw@roboseasy' in warn[0] and 'namin@namin-Legion' in warn[0]
	assert 'physical-labs-lekiwi' not in warn[0]
	assert 'AAAAC3NzaKHW99' not in done.stdout and 'AAAAB3NzaNAMIN' not in done.stdout


# --- 부트 파티션 원시 장치 ([1])

def test_router_value_left_on_raw_boot_partition_fails(tree: CheckTree) -> None:
	tree.write('etc/NetworkManager/system-connections/home-wifi.nmconnection', HOME_KEYFILE, 0o600)
	tree.write('dev/mmcblk0p1', 'FAT32\x00\x00 password: "FAKEPSK-12345"\x00\x00\nfree\nFAKEPSK-12345\n')
	done = tree.check()
	assert done.returncode == 1
	fails = [line for line in lines(done, 'FAIL') if '부트 파티션' in line]
	assert fails and '2' in fails[0], done.stdout                  # 개수만 보인다
	assert 'FAKEPSK' not in done.stdout + done.stderr


def test_unreadable_boot_partition_warns(tree: CheckTree) -> None:
	tree.write('etc/NetworkManager/system-connections/home-wifi.nmconnection', HOME_KEYFILE, 0o600)
	(tree.root / 'dev/mmcblk0p1').unlink()
	done = tree.check()
	assert done.returncode == 0
	assert_flagged(done, 'WARN', '부트 파티션')


# --- 처음 AP 기본값 (lekiwi01 / lekiwi01)

def test_default_ap_password_is_not_traced(tree: CheckTree) -> None:
	"""이름과 같은 공개 기본값(lekiwi01)은 로그 · 설정 곳곳에 이름으로 있어 추적하지 않는다."""
	tree.write('var/log/syslog', 'NetworkManager: activating lekiwi01\n')
	tree.write(f'{H}/.bash_history', 'nmcli dev wifi connect lekiwi01\n')
	done = tree.check()
	assert done.returncode == 0, done.stdout
	assert not [line for line in done.stdout.splitlines() if 'AP 비밀번호 값' in line]
	assert [line for line in lines(done, 'INFO') if '공개 기본값' in line], done.stdout


def test_ap_password_equal_to_its_name_is_not_traced(tree: CheckTree) -> None:
	set_ap(tree, 'lekiwi-lab', 'lekiwi-lab')                      # provision --ap-ssid lekiwi-lab 의 꼴
	tree.write('var/log/syslog', 'hostapd: lekiwi-lab up\n')
	done = tree.check()
	assert lines(done, 'FAIL') == [], done.stdout


@pytest.mark.parametrize('name, password', [('lekiwi01', AP_PSK), ('lekiwi-lab', 'lekiwi01'), ('lekiwi-lab', 'lekiwi-lab')])
def test_ap_not_factory_default_warns(tree: CheckTree, name: str, password: str) -> None:
	set_ap(tree, name, password)
	done = tree.check()
	assert done.returncode == 0, done.stdout
	assert_flagged(done, 'WARN', '처음 AP 기본값')
	assert AP_PSK not in done.stdout + done.stderr


# --- 흔한 글자 · 시스템 폴더의 개인키 · authorized_keys · 로봇별 값 · 셸 기록

def test_common_strings_are_not_traced(tmp_path: Path) -> None:
	"""/etc/passwd · group · hostname에 있는 값(사용자 · 그룹 · 호스트 이름)은 곳곳에 있어 값 추적에서 뺀다.
	줄 모양 FAIL은 그대로 파일 이름을 대고, 값은 출력에도 명령줄에도 나오지 않는다."""
	grep = shutil.which('grep')
	tree = CheckTree(tmp_path, {'grep': f'echo "grep $*" >> "$FAKE_LOG"\nexec {grep} "$@"\n'})
	tree.write('etc/group', 'root:x:0:\nroboseasy:x:1000:\nrobotlab9:x:1001:roboseasy\n')
	tree.write(f'{H}/notes.txt', 'password: robotlab9\n')
	tree.write('var/log/auth.log', 'groupadd[812]: group added to /etc/group: name=robotlab9\n')
	done = tree.check()
	assert_flagged(done, 'FAIL', f'{HOME}/notes.txt')
	assert not [line for line in lines(done, 'FAIL') if 'auth.log' in line or '/etc/group' in line], done.stdout
	assert [line for line in lines(done, 'INFO') if '흔한 글자라 건너뜀' in line and f'{HOME}/notes.txt' in line], done.stdout
	assert 'robotlab9' not in done.stdout + done.stderr
	assert not [line for line in tree.fake.calls() if 'robotlab9' in line]


PEM_KEY = '-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqFAKEKEYBODY\n-----END PRIVATE KEY-----\n'


@pytest.mark.parametrize('rel', ['var/lib/fwupd/pki/secret.key', 'etc/ssl/private/lab.key', 'usr/local/etc/x.pem',
	'opt/tool/id_service', 'var/lib/foo/client.key'])
def test_private_keys_in_system_folders_fail(tree: CheckTree, rel: str) -> None:
	"""복제한 모든 로봇이 같은 개인키를 갖게 된다. /etc/ssh 호스트키만 뺀다 (이미지 도구가 지우고 첫 부팅에 새로 만든다)."""
	tree.write(rel, PEM_KEY, 0o600)
	tree.write('etc/ssh/ssh_host_rsa_key', PEM_KEY.replace('BEGIN PRIVATE', 'BEGIN RSA PRIVATE'), 0o600)
	done = tree.check()
	assert done.returncode == 1
	assert_flagged(done, 'FAIL', '/' + rel)
	assert 'ssh_host_rsa_key' not in done.stdout
	assert 'FAKEKEYBODY' not in done.stdout


def test_binary_with_key_header_text_is_not_a_key(tree: CheckTree) -> None:
	"""실행 파일 속 문자열은 키 파일이 아니다 (PEM 키는 글자 파일)."""
	tree.write('usr/local/lib/libfoo.so', 'ELF\0\0\0-----BEGIN PRIVATE KEY-----\0\0')
	assert tree.check().returncode == 0


def test_authorized_keys_counts_distinct_app_keys(tree: CheckTree) -> None:
	"""앱 설치 키는 줄 수와 함께 키 본문으로 겹침을 뺀 수도 보인다 (본문 · 지문은 찍지 않는다)."""
	tree.write(f'{H}/.ssh/authorized_keys', 'ssh-ed25519 AAAAPHYSone physical-labs-lekiwi\n' * 3
		+ 'ssh-ed25519 AAAAPHYStwo physical-labs-lekiwi\nssh-ed25519 AAAAKHWkey khw@roboseasy\n', 0o600)
	done = tree.check()
	warn = [line for line in lines(done, 'WARN') if 'authorized_keys' in line]
	assert warn and '키 5개 = 앱 설치 4 (서로 다른 키 2개) + 개인 1 (khw@roboseasy)' in warn[0], done.stdout
	assert 'AAAA' not in done.stdout


@pytest.mark.parametrize('rel', [f'{H}/.bash_history', 'root/.bash_history', f'{H}/.python_history'])
def test_shell_history_warns(tree: CheckTree, rel: str) -> None:
	"""이미지 도구가 복사본에서 지우지만, 비밀번호가 든 명령이 있을 수 있어 알린다."""
	tree.write(rel, 'ls -la\n')
	done = tree.check()
	assert done.returncode == 0, done.stdout
	assert [line for line in lines(done, 'WARN') if '셸 기록' in line and '/' + rel in line], done.stdout


def test_empty_shell_history_is_ok(tree: CheckTree) -> None:
	tree.write(f'{H}/.bash_history', '')
	done = tree.check()
	assert not [line for line in lines(done, 'WARN') if '셸 기록' in line], done.stdout
