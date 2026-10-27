"""clone_init.sh 시험: 복제한 카드마다 한 번 하는 초기화 (NetworkManager 세대).

임시 CLONE_ROOT에 골든 카드 모양 트리를 만들고 가짜 PATH(test_robot_net의 FakeBin, `hostnamectl`도 stub)에서 돌린다.
clone_init.sh는 ~/lekiwi_net에 설치되는 스크립트라 같은 폴더의 set_ap.sh · nm_kf.py · set_ip.sh를 쓴다.
"""

import configparser
import subprocess
from pathlib import Path

import pytest

from test_robot_net import AP_KEYFILE, HOME_KEYFILE, ROBOT_NET, FakeBin, index_of, tree_state

CLONE_INIT = ROBOT_NET / 'clone_init.sh'

# 로봇(lekiwi04)의 /etc/hosts 모양 (cloud-init이 만든 것)
HOSTS = '''# Your system has configured 'manage_etc_hosts' as True.
# As a result, if you wish for changes to this file to persist
# then you will need to either
# a.) make changes to the master file in /etc/cloud/templates/hosts.debian.tmpl
# b.) change or remove the value of 'manage_etc_hosts' in
#     /etc/cloud/cloud.cfg or cloud-config from user-data
#
127.0.1.1 lekiwi04 roboseasy-pi02
127.0.0.1 localhost

# The following lines are desirable for IPv6 capable hosts
::1 localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
'''


# 처음 AP: 모든 로봇이 이름 · 비밀번호 lekiwi01 (사용자 결정)
DEFAULT_AP = AP_KEYFILE.replace('ssid=lekiwi04', 'ssid=lekiwi01').replace('psk=FAKEAPPSK-1', 'psk=lekiwi01')


class CloneTree:
	"""골든 카드를 구운 직후 모양 (호스트 이름 lekiwi04, 킷 4, AP 프로필만, 공유기 프로필 없음)."""

	def __init__(self, base: Path) -> None:
		self.fake = FakeBin(base, {'hostnamectl': 'echo "hostnamectl $*" >> "$FAKE_LOG"\n'})
		self.root = base / 'root'
		self.kf_dir = self.root / 'etc/NetworkManager/system-connections'
		self.write('etc/hostname', 'lekiwi04\n')
		self.write('etc/hosts', HOSTS)
		self.write('etc/kit-number', '4\n')
		self.write('etc/cloud/cloud.cfg.d/99-fake-cloud.cfg', 'datasource_list: [ NoCloud, None ]\n')
		self.write('etc/NetworkManager/system-connections/lekiwi-ap.nmconnection', DEFAULT_AP, 0o600)
		(self.root / 'var/lib').mkdir(parents=True)

	def write(self, rel: str, text: str = 'x\n', mode: int = 0o644) -> None:
		path = self.root / rel
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text)
		path.chmod(mode)

	def text(self, rel: str) -> str:
		return (self.root / rel).read_text()

	def keyfile(self, name: str) -> configparser.RawConfigParser:
		parser = configparser.RawConfigParser(delimiters=('=',), interpolation=None)
		parser.optionxform = str
		parser.read(self.kf_dir / f'{name}.nmconnection')
		return parser

	def clone_init(self, *args: str, **env: str) -> subprocess.CompletedProcess:
		return self.fake.run([str(CLONE_INIT), *args], CLONE_ROOT=str(self.root), **env)


@pytest.fixture
def tree(tmp_path: Path) -> CloneTree:
	return CloneTree(tmp_path)


def ok(done: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
	assert done.returncode == 0, done.stdout + done.stderr
	return done


@pytest.mark.parametrize('args', [
	('lekiwi07', '4'), ('lekiwi7', '7'), ('robot07', '7'), ('lekiwi00', '0'), ('lekiwi55', '55'), ('lekiwi07', 'x'),
	('lekiwi07',), ('lekiwi07', '7', 'abcd1234'),                                 # 옛 세 번째 인자는 없앴다
	('lekiwi07', '7', '--ap-password', 'short'), ('lekiwi07', '7', '--ap-password', '한글비밀번호입니다요'),
	('lekiwi07', '7', '--ap-password', ' abcd1234'), ('lekiwi07', '7', '--ap-password', 'a' * 64),
	('lekiwi07', '7', '--ap-password', ''), ('lekiwi07', '7', '--ap-password'), ('lekiwi07', '7', '--ap-ssid', 'a;b'),
	('lekiwi07', '7', '--ap-ssid', 'a' * 33), ('lekiwi07', '7', '--ap-ssid', ' lab'), ('lekiwi07', '7', '--bogus', 'x'),
])
def test_clone_init_rejects_bad_arguments(tree: CloneTree, args: tuple[str, ...]) -> None:
	before = tree_state(tree.root)
	done = tree.clone_init(*args)
	assert done.returncode == 1
	assert tree_state(tree.root) == before
	assert tree.fake.calls() == []


def test_clone_init_sets_identity_files(tree: CloneTree) -> None:
	ok(tree.clone_init('lekiwi07', '7'))
	assert tree.text('etc/hostname') == 'lekiwi07\n'
	assert tree.text('etc/kit-number') == '7\n'
	assert tree.text('etc/hosts') == HOSTS.replace('127.0.1.1 lekiwi04 roboseasy-pi02', '127.0.1.1 lekiwi07')
	assert tree.text('etc/cloud/cloud.cfg.d/99_preserve_hostname.cfg') == 'preserve_hostname: true\n'
	assert 'hostnamectl set-hostname lekiwi07' in tree.fake.calls()


def test_clone_init_adds_hosts_line_when_missing(tree: CloneTree) -> None:
	tree.write('etc/hosts', '127.0.0.1 localhost\n')
	ok(tree.clone_init('lekiwi07', '7'))
	assert tree.text('etc/hosts') == '127.0.0.1 localhost\n127.0.1.1 lekiwi07\n'


@pytest.mark.parametrize('kit, channel', [('3', '1'), ('7', '6'), ('5', '11'), ('54', '1')])
def test_clone_init_sets_channel_and_keeps_default_ap(tree: CloneTree, kit: str, channel: str) -> None:
	"""처음 AP 이름 · 비밀번호(lekiwi01)는 그대로, 채널만 카드마다 (1, 6, 11)[N % 3]."""
	done = ok(tree.clone_init(f'lekiwi{int(kit):02d}', kit))
	ap = tree.keyfile('lekiwi-ap')
	assert (ap.get('wifi', 'ssid'), ap.get('wifi-security', 'psk')) == ('lekiwi01', 'lekiwi01')
	assert (ap.get('wifi', 'channel'), ap.get('wifi', 'band')) == (channel, 'bg')
	assert f'nmcli con load {tree.kf_dir}/lekiwi-ap.nmconnection' in tree.fake.calls()
	assert (tree.kf_dir / 'lekiwi-ap.nmconnection').stat().st_mode & 0o777 == 0o600
	assert '비밀번호' not in done.stdout


def test_clone_init_sets_ap_name_and_password_only_when_given(tree: CloneTree) -> None:
	ok(tree.clone_init('lekiwi07', '7', '--ap-ssid', 'lekiwi-lab'))
	ap = tree.keyfile('lekiwi-ap')
	assert (ap.get('wifi', 'ssid'), ap.get('wifi-security', 'psk')) == ('lekiwi-lab', 'lekiwi01')
	done = ok(tree.clone_init('lekiwi07', '7', '--ap-password', 'p@ss w&rd$1'))
	ap = tree.keyfile('lekiwi-ap')
	assert (ap.get('wifi', 'ssid'), ap.get('wifi-security', 'psk')) == ('lekiwi-lab', 'p@ss w&rd$1')
	assert 'p@ss w&rd$1' not in done.stdout + done.stderr


def test_clone_init_keeps_ap_password_out_of_sudo_logs(tree: CloneTree) -> None:
	"""비밀번호를 중첩 sudo(set_ap.sh) 명령줄로 넘기면 auth.log에 남는다: nm_kf.py를 직접 부른다."""
	ok(tree.clone_init('lekiwi07', '7', '--ap-password', 'FAKEPW-00001'))
	assert tree.keyfile('lekiwi-ap').get('wifi-security', 'psk') == 'FAKEPW-00001'
	assert not [line for line in tree.fake.calls() if line.startswith('sudo ')]
	assert not [line for line in tree.fake.calls() if 'FAKEPW' in line]


def test_clone_init_fixed_ip_when_home_profile_exists(tree: CloneTree) -> None:
	ok(tree.clone_init('lekiwi07', '7'))
	assert not (tree.kf_dir / 'home-wifi.nmconnection').exists()          # 공유기 프로필이 없으면 만들지 않는다
	dhcp = HOME_KEYFILE.replace('method=manual\naddress1=192.168.0.204/24,192.168.0.1\ndns=8.8.8.8;1.1.1.1;\n',
		'method=auto\n')
	tree.write('etc/NetworkManager/system-connections/home-wifi.nmconnection', dhcp, 0o600)
	ok(tree.clone_init('lekiwi07', '7', FAKE_ACTIVE='lekiwi-ap'))
	home = tree.keyfile('home-wifi')
	assert home.get('ipv4', 'method') == 'manual'
	assert home.get('ipv4', 'address1') == '192.168.0.207/24,192.168.0.1'


def test_clone_init_removes_state_dir(tree: CloneTree) -> None:
	tree.write('var/lib/lekiwi-net/home-wifi.prev', HOME_KEYFILE, 0o600)
	ok(tree.clone_init('lekiwi07', '7'))
	assert not (tree.root / 'var/lib/lekiwi-net').exists()


def test_clone_init_is_idempotent(tree: CloneTree) -> None:
	ok(tree.clone_init('lekiwi07', '7'))
	first = tree_state(tree.root)
	ok(tree.clone_init('lekiwi07', '7'))
	assert tree_state(tree.root) == first
	assert tree.text('etc/hosts').count('127.0.1.1') == 1


def test_clone_init_restarts_ap_when_it_is_up(tree: CloneTree) -> None:
	"""켜져 있던 AP만 새 채널로 다시 올린다."""
	ok(tree.clone_init('lekiwi07', '7', FAKE_ACTIVE='lekiwi-ap'))
	calls = tree.fake.calls()
	assert index_of(calls, f'nmcli con load {tree.kf_dir}/lekiwi-ap.nmconnection') < index_of(calls, 'nmcli con up lekiwi-ap')
	ok(tree.clone_init('lekiwi07', '7'))
	assert sum(line == 'nmcli con up lekiwi-ap' for line in tree.fake.calls()) == 1
