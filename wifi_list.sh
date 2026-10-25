#!/usr/bin/env bash
# 주변 와이파이 목록을 보기 좋게 출력한다 (사용: wifi list [인터페이스]).
#
# iw 로 스캔하므로 NetworkManager 와 상관없다. 스캔은 root 권한이 필요해 sudo 로 부른다.
# 보안 표기(RSN → WPA2/3, WPA, Privacy → WEP, 아니면 개방)는 게이트웨이(network.py 의 parse_iw_scan)와 같고,
# 신호 임계값(-55/-67/-78)은 Physical Labs 앱이 이 스크립트와 같게 쓴다. 숨김 SSID 는 조금 다르다:
# 여기서는 \x00 이 하나라도 있으면 숨김, 게이트웨이는 비었거나 전부 \x00 일 때만 숨김이다. 여기를 바꾸면 그쪽도 본다.
set -u

# 무선 인터페이스를 고정하지 않고 찾는다 (보통 wlan0).
IFACE="${1:-$(iw dev 2>/dev/null | awk '$1=="Interface"{print $2; exit}')}"
if [ -z "$IFACE" ]; then
	echo "무선 인터페이스를 찾지 못했습니다." >&2
	exit 1
fi

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

SCAN="$($SUDO iw dev "$IFACE" scan 2>/dev/null)"
if [ -z "$SCAN" ]; then
	echo "스캔에 실패했습니다 ($IFACE). 무선이 꺼져 있거나 권한이 없습니다." >&2
	exit 1
fi

printf '%s\n' "$SCAN" | awk -v iface="$IFACE" '
function flush(   band, sec, bars, lvl) {
    if (ssid == "" || ssid ~ /\\x00/) ssid = "(숨김)"
    if (bss == "") return
    # 같은 SSID 는 가장 센 신호만 남긴다 (공유기 여러 대 / 2.4+5GHz 중복 제거)
    if (!(ssid in best) || sig > best[ssid]) {
        best[ssid] = sig; bfreq[ssid] = freq
        bsec[ssid] = (rsn ? "WPA2/3" : (wpa ? "WPA" : (priv ? "WEP" : "개방")))
    }
}
/^BSS /      { flush(); bss=substr($2,1,17); ssid=""; sig=0; freq=0; rsn=0; wpa=0; priv=0 }
/^\tfreq:/   { freq=$2+0 }
/^\tsignal:/ { sig=$2+0 }
/^\tcapability:/ { if ($0 ~ /Privacy/) priv=1 }
/^\tRSN:/    { rsn=1 }
/^\tWPA:/    { wpa=1 }
/^\tSSID:/   { s=$0; sub(/^\tSSID: /,"",s); ssid=s }
END {
    flush()
    n = 0
    for (s in best) { n++; order[n] = s }
    # 신호 세기 내림차순 정렬
    for (i = 1; i <= n; i++)
        for (j = i+1; j <= n; j++)
            if (best[order[j]] > best[order[i]]) { t=order[i]; order[i]=order[j]; order[j]=t }

    printf "\n  %-30s %8s  %-6s %-9s %s\n", "SSID", "신호", "세기", "대역", "보안"
    printf "  %s\n", "----------------------------------------------------------------------"
    for (i = 1; i <= n; i++) {
        s = order[i]; g = best[s]; f = bfreq[s]
        band = (f >= 5000 ? "5GHz" : "2.4GHz")
        if      (g >= -55) bars = "████"
        else if (g >= -67) bars = "███ "
        else if (g >= -78) bars = "██  "
        else               bars = "█   "
        printf "  %-30s %5d dBm  %-6s %-9s %s\n", s, g, bars, band, bsec[s]
    }
    printf "\n  %s (%d개)\n\n", iface, n
}'
