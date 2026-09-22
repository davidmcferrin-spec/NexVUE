<?php
/**
 * nexvue-ops.php — JSON API for NexVUE Services + Channels ops UI.
 *
 * Phase 2 local auth: session cookie required. Roles:
 *   admin — Services + Settings + kick + branding + support/update + host reboot + card/slots + public reachability + certificates + Cloudflare TURN
 *   operator — Settings + kick + branding (not public hostname/IP, not certificates, not Cloudflare TURN)
 *   any auth (user or share) — aliases, kick_check (Player/Multiview)
 *
 * Privileged work goes through allowlisted sudo wrappers only
 * (see nexvue-ops.sudoers / /usr/local/bin/nexvue-ops-*.sh).
 *
 * Actions (GET or POST JSON body):
 *   services | journal | journal_clear | audio_probe | channels_list | channel_get | channel_put
 *   | channels_bulk | restart | restart_encoders | set_enabled | set_running | aliases
 *   | kick_viewer | kick_check | logo_get | logo_put | logo_delete | support_bundle
 *   | update_status | update_repo | update_setup_log | reboot_host | network_get | network_put | network_test
 *   | hardware_get | hardware_put
 *   | tls_status | tls_issue | tls_upload
 *   | turn_get | turn_put | turn_test
 *   | sfu_get | sfu_put | sfu_test
 *
 * support_bundle returns application/zip (not JSON): builds a redacted
 * journals+config+state zip via nexvue-ops-support-bundle.sh for the
 * requested hours window (1|6|12|24|48|72).
 *
 * tls_status / tls_issue / tls_upload call nexvue-ops-tls.sh. Issue uses lego
 * TLS-ALPN-01 on :443 (Apache stops for the challenge; WHEP :8889 stays up).
 * The cert name is Settings → Public hostname (NEXVUE_PUBLIC_HOSTNAME);
 * NEXVUE_TLS_DOMAIN is a write-through alias. Port 80 is never used. Upload
 * installs PEMs to /etc/nexvue/tls and reloads apache2 + mediamtx.
 * Status, issue, and upload are admin-only (panel hidden from operators).
 *
 * network_test is an advisory probe (DNS A, this box's WAN IPv4, TCP 443/8889
 * on the typed name or IP). It does not write config and never blocks Save.
 * Hairpin NAT can make the TCP checks fail even when off-site viewers work.
 *
 * turn_get / turn_put / turn_test persist Cloudflare Realtime TURN in auth.db
 * (never nexvue.env). Admin-only, same gate as Public reachability. Turn on
 * mints short-lived iceServers for Player / Multiview via whep_jwt; MediaMTX
 * is not restarted.
 *
 * hardware_get / hardware_put persist MAX_DEVICES + MAX_CHANNELS (same
 * value) in nexvue.env, seed missing channel .env files, and enable/disable
 * nexvue-encode@N. Admin-only, same gate as Public reachability. Detect
 * reports how many DeckLink sub-devices the status daemon currently sees.
 *
 * sfu_get / sfu_put / sfu_test persist Cloudflare Stream (WHIP/WHEP) in
 * auth.db. Admin-only. Mode off | hybrid (share + portal only) | sfu (all
 * viewers). Hybrid keeps LAN Player on local MediaMTX so control rooms stay
 * on-box; off-site shares do not multiply the 1 Gb NIC. TURN is unchanged.
 *
 * update_status / update_repo call nexvue-ops-update.sh (git fetch + hard-reset
 * to origin/NEXVUE_UPDATE_BRANCH + setup.sh). Admin-only — same gate as
 * Services (operators cannot poll status or apply).
 *
 * reboot_host calls nexvue-ops-reboot.sh → systemctl reboot (clean systemd
 * shutdown, not reboot -f). Admin-only. The helper takes no arguments;
 * sudoers omit a trailing * so extra argv cannot sneak through. JSON
 * {"ok":true} is best-effort — the HTTP connection usually dies when the
 * kernel goes down.
 *
 * journal_clear records a per-unit watermark via nexvue-ops-journal.sh clear
 * so the Services journal view hides prior lines for that unit only (systemd
 * cannot purge one unit from the binary journal; host-wide vacuum is gone).
 *
 * audio_probe stops nexvue-encode@N if running, runs decklink-audio-probe on
 * the channel DEVICE_NUMBER (8ch PCM energy), restarts the unit, and returns
 * per-embed levels + AUDIO_LAYOUT suggestions — operator confirms in Settings.
 *
 * restart_encoders restarts every systemd-enabled nexvue-encode@N (parked /
 * disabled slots are left alone). set_enabled toggles systemd enable/disable
 * (with --now); set_running is
 * runtime start/stop (boot config untouched). Both apply to encoder units
 * ONLY (nexvue-encode@0-7) via nexvue-ops-enable.sh — the LAN-trust ops page
 * must not be able to disable or stop mediamtx or the shared daemons.
 *
 * kick_viewer POSTs to MediaMTX /v3/webrtcsessions/kick/{id} on loopback
 * (no sudo), then records the session in a short-lived kick registry so
 * Player / Multiview can suppress self-healing reconnect. Used by
 * Metrics → Viewer sessions. Phase 1 LAN-trust — not a rejoin ban (Phase 2 auth).
 *
 * CLI include: when PHP_SAPI is cli and NEXVUE_OPS_HTTP is unset, this file
 * only defines helpers (for unit tests) and returns without dispatching.
 */

declare(strict_types=1);

require_once __DIR__ . '/nexvue-auth-lib.php';

const CHANNELS_DIR = '/etc/nexvue/channels';
/** Hard ceiling for encode@N allowlists (Quad 2). Live count is auth_max_channel_id(). */
const MAX_CHANNEL_ID = 7;
const SUDO = '/usr/bin/sudo';
/** Kick registry TTL — long enough for the 5s player reconnect window + retries. */
const KICK_REGISTRY_TTL_S = 600;
const KICK_REASON_MAX_LEN = 200;
/** Station branding logo — raw bytes + JSON metadata under /var/lib/nexvue/branding. */
const LOGO_MAX_BYTES = 1048576;
/** PEM upload cap (cert or key) — leaf+chain is typically a few KB. */
const TLS_PEM_MAX_BYTES = 131072;
const LOGO_ALLOWED_MIMES = [
    'image/png' => true,
    'image/jpeg' => true,
    'image/webp' => true,
];

const EDITABLE_KEYS = [
    'CHANNEL_ALIAS', 'INPUT_TYPE', 'SRT_URI', 'SRT_LATENCY_MS',
    'DEINT_FIELDS', 'DEINT_METHOD', 'HI_PRESET', 'BITRATE_KBPS', 'GOP_FRAMES',
    'ENABLE_AUDIO', 'AUDIO_FRAME_MS', 'AUDIO_BITRATE_BPS', 'AUDIO_CHANNELS', 'AUDIO_LAYOUT', 'AUDIO_EMBEDS',
    'DECKLINK_BUFFER_FRAMES', 'VIDEO_ENCODER', 'EXTRA_ENC_ARGS',
    'LO_ENABLE', 'LO_PRESET', 'LO_WIDTH', 'LO_HEIGHT', 'LO_BITRATE_KBPS', 'LO_FPS',
    'LO_TARGET_USAGE', 'LO_QUEUE_BUFFERS',
    'AUTO_PARK_UNLOCK_CYCLES',
    'AUTO_UNPARK',
];

function fail(int $status, string $message): never {
    if (!headers_sent()) {
        header('Content-Type: application/json');
        header('Cache-Control: no-store');
    }
    http_response_code($status);
    echo json_encode(['ok' => false, 'error' => $message]);
    exit;
}

function ops_setup_log_path(): string {
    $override = getenv('NEXVUE_SETUP_LOG');
    if (is_string($override) && $override !== '') {
        return $override;
    }
    $data = getenv('NEXVUE_DATA');
    if (is_string($data) && $data !== '') {
        return rtrim($data, '/\\') . '/update-setup.log';
    }
    return '/var/lib/nexvue/update-setup.log';
}

function ops_setup_state_path(): string {
    $override = getenv('NEXVUE_SETUP_STATE');
    if (is_string($override) && $override !== '') {
        return $override;
    }
    $log = ops_setup_log_path();
    if (str_ends_with($log, '.log')) {
        return substr($log, 0, -4) . '.state';
    }
    return $log . '.state';
}

function ops_strip_ansi(string $s): string {
    $s = preg_replace('/\x1B\[[0-9;]*[A-Za-z]/', '', $s) ?? $s;
    return str_replace("\r", '', $s);
}

/** @return array{ok:bool|null, at:string} */
function ops_read_setup_state(): array {
    $path = ops_setup_state_path();
    $out = ['ok' => null, 'at' => ''];
    if (!is_readable($path)) {
        return $out;
    }
    $line = trim((string)@file_get_contents($path));
    if ($line === '') {
        return $out;
    }
    $parts = preg_split('/\s+/', $line, 2) ?: [];
    $status = $parts[0] ?? '';
    if ($status === 'OK') {
        $out['ok'] = true;
    } elseif ($status === 'FAIL') {
        $out['ok'] = false;
    }
    $out['at'] = $parts[1] ?? '';
    return $out;
}

function ops_setup_log_tail(int $maxLines = 40): string {
    $path = ops_setup_log_path();
    if (!is_readable($path)) {
        return '';
    }
    $lines = @file($path, FILE_IGNORE_NEW_LINES);
    if (!is_array($lines) || $lines === []) {
        return '';
    }
    $slice = array_slice($lines, -$maxLines);
    return ops_strip_ansi(implode("\n", $slice));
}

function ops_setup_log_text(int $maxBytes = 200000): array {
    $path = ops_setup_log_path();
    if (!is_readable($path)) {
        return ['log' => '', 'truncated' => false];
    }
    $size = @filesize($path);
    $size = is_int($size) ? $size : 0;
    $truncated = $size > $maxBytes;
    if (!$truncated) {
        $raw = (string)@file_get_contents($path);
        return ['log' => ops_strip_ansi($raw), 'truncated' => false];
    }
    $fh = @fopen($path, 'rb');
    if ($fh === false) {
        return ['log' => '', 'truncated' => true];
    }
    fseek($fh, -$maxBytes, SEEK_END);
    $raw = (string)stream_get_contents($fh);
    fclose($fh);
    $nl = strpos($raw, "\n");
    if ($nl !== false) {
        $raw = substr($raw, $nl + 1);
    }
    return ['log' => ops_strip_ansi($raw), 'truncated' => true];
}

/**
 * @param array<string,mixed> $parsed
 * @return array<string,mixed>
 */
function ops_merge_setup_log_fields(array $parsed): array {
    $st = ops_read_setup_state();
    if (!array_key_exists('last_setup_ok', $parsed) || $parsed['last_setup_ok'] === null) {
        $parsed['last_setup_ok'] = $st['ok'];
    }
    if (!isset($parsed['last_setup_at']) || $parsed['last_setup_at'] === '') {
        $parsed['last_setup_at'] = $st['at'];
    }
    if (!isset($parsed['setup_tail']) || $parsed['setup_tail'] === '') {
        $parsed['setup_tail'] = ops_setup_log_tail();
    }
    return $parsed;
}

function kick_is_uuid(string $id): bool {
    return (bool)preg_match(
        '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i',
        $id
    );
}

function kick_registry_path(): string {
    $override = getenv('NEXVUE_KICK_REGISTRY');
    if (is_string($override) && $override !== '') {
        return $override;
    }
    return sys_get_temp_dir() . '/nexvue-kicked-sessions.json';
}

function kick_normalize_reason(mixed $reason): string {
    if (!is_string($reason)) {
        return '';
    }
    $reason = trim($reason);
    $reason = preg_replace('/[\x00-\x1F\x7F]/u', '', $reason) ?? '';
    if (strlen($reason) > KICK_REASON_MAX_LEN) {
        $reason = substr($reason, 0, KICK_REASON_MAX_LEN);
    }
    return $reason;
}

/** Strip :port from MediaMTX remoteAddr (IPv4 host:port or [IPv6]:port). */
function kick_strip_ip_port(string $addr): string {
    $addr = trim($addr);
    if ($addr === '') {
        return '';
    }
    if (str_starts_with($addr, '[')) {
        $end = strpos($addr, ']');
        if ($end !== false) {
            return substr($addr, 1, $end - 1);
        }
    }
    if (substr_count($addr, ':') === 1) {
        return explode(':', $addr, 2)[0];
    }
    return $addr;
}

function kick_registry_prune(array $entries): array {
    $cut = time() - KICK_REGISTRY_TTL_S;
    $out = [];
    foreach ($entries as $e) {
        if (!is_array($e)) {
            continue;
        }
        $ts = (int)($e['ts'] ?? 0);
        if ($ts >= $cut && is_string($e['session_id'] ?? null) && kick_is_uuid($e['session_id'])) {
            $out[] = $e;
        }
    }
    return $out;
}

/**
 * Exclusive flock around read-modify-write of the kick registry JSON file.
 * $fn receives pruned entries and returns ['entries' => array, 'return' => mixed]
 * to persist, or any other value to leave the file rewritten with pruned entries only.
 */
function kick_registry_with_lock(callable $fn): mixed {
    $path = kick_registry_path();
    $fh = @fopen($path, 'c+');
    if ($fh === false) {
        throw new RuntimeException('cannot open kick registry');
    }
    try {
        if (!flock($fh, LOCK_EX)) {
            throw new RuntimeException('cannot lock kick registry');
        }
        rewind($fh);
        $raw = stream_get_contents($fh);
        $entries = [];
        if (is_string($raw) && $raw !== '') {
            $decoded = json_decode($raw, true);
            if (is_array($decoded)) {
                $entries = $decoded;
            }
        }
        $entries = kick_registry_prune($entries);
        $result = $fn($entries);
        $toWrite = $entries;
        $ret = $result;
        if (is_array($result) && array_key_exists('entries', $result)) {
            $toWrite = $result['entries'];
            $ret = $result['return'] ?? null;
        }
        $json = json_encode(array_values($toWrite), JSON_UNESCAPED_SLASHES);
        if ($json === false) {
            $json = '[]';
        }
        ftruncate($fh, 0);
        rewind($fh);
        fwrite($fh, $json);
        fflush($fh);
        return $ret;
    } finally {
        flock($fh, LOCK_UN);
        fclose($fh);
    }
}

function kick_registry_add(string $sessionId, string $ip, string $reason): void {
    kick_registry_with_lock(static function (array $entries) use ($sessionId, $ip, $reason): array {
        $kept = [];
        foreach ($entries as $e) {
            if (($e['session_id'] ?? '') !== $sessionId) {
                $kept[] = $e;
            }
        }
        $kept[] = [
            'session_id' => $sessionId,
            'ip' => $ip,
            'reason' => $reason,
            'ts' => time(),
        ];
        return ['entries' => $kept, 'return' => null];
    });
}

function kick_registry_remove(string $sessionId): void {
    kick_registry_with_lock(static function (array $entries) use ($sessionId): array {
        $kept = [];
        foreach ($entries as $e) {
            if (($e['session_id'] ?? '') !== $sessionId) {
                $kept[] = $e;
            }
        }
        return ['entries' => $kept, 'return' => null];
    });
}

/**
 * Look up a kick by MediaMTX WebRTC session UUID only.
 * Multiple viewers can share an IP (NAT) and even the same channel — each has
 * a distinct session_id, so kicking one never matches the others. No IP
 * fallback: a missing session_id fails open (self-heal) rather than
 * suppressing every peer behind the same REMOTE_ADDR.
 *
 * @return array{kicked: bool, reason?: string}
 */
function kick_registry_check(?string $sessionId, ?string $clientIp = null): array {
    return kick_registry_with_lock(static function (array $entries) use ($sessionId): array {
        $match = null;
        if (is_string($sessionId) && $sessionId !== '') {
            foreach ($entries as $e) {
                if (($e['session_id'] ?? '') === $sessionId) {
                    $match = $e;
                    break;
                }
            }
        }
        if ($match === null) {
            return ['entries' => $entries, 'return' => ['kicked' => false]];
        }
        $reason = (string)($match['reason'] ?? '');
        // Escape for safety if a client ever uses innerHTML; textContent is still preferred.
        $reasonOut = htmlspecialchars($reason, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
        return [
            'entries' => $entries,
            'return' => ['kicked' => true, 'reason' => $reasonOut],
        ];
    });
}

/** Units the ops UI may enable/disable — encoders only, never shared services. */
function unit_enable_allowed(string $unit): bool {
    return (bool)preg_match('/^nexvue-encode@[0-7]$/', $unit);
}

function ops_max_channels(): int {
    return auth_max_channels();
}

function ops_max_channel_id(): int {
    return auth_max_channel_id();
}

function channel_id_ok($id): bool {
    return is_numeric($id) && (int)$id >= 0 && (int)$id <= ops_max_channel_id();
}

/**
 * Live DeckLink sub-device count from the status daemon (same loopback
 * :9998 the player uses). Null count when the daemon is down or stale.
 *
 * @return array{count: ?int, names: list<string>, stale: bool}
 */
function hardware_detect_devices(): array {
    $empty = ['count' => null, 'names' => [], 'stale' => false];
    $env = getenv('NEXVUE_STATUS_URL');
    $urls = (is_string($env) && $env !== '')
        ? [rtrim($env, '/') . '/status']
        : ['http://127.0.0.1:9998/status', 'https://127.0.0.1:9998/status'];
    foreach ($urls as $url) {
        $body = hardware_fetch_status($url);
        if ($body === null) {
            continue;
        }
        $data = json_decode($body, true);
        if (!is_array($data) || !isset($data['devices']) || !is_array($data['devices'])) {
            continue;
        }
        $names = [];
        foreach ($data['devices'] as $d) {
            if (is_array($d) && isset($d['name']) && is_string($d['name'])) {
                $names[] = $d['name'];
            } elseif (is_array($d) && isset($d['index'])) {
                $names[] = 'SDI ' . (string)$d['index'];
            }
        }
        $stale = !empty($data['stale']);
        return [
            'count' => $stale ? null : count($data['devices']),
            'names' => $names,
            'stale' => $stale,
        ];
    }
    return $empty;
}

function hardware_fetch_status(string $url): ?string {
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        if ($ch === false) {
            return null;
        }
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 1,
            CURLOPT_TIMEOUT => 3,
            CURLOPT_SSL_VERIFYPEER => false,
            CURLOPT_SSL_VERIFYHOST => 0,
            CURLOPT_HTTPHEADER => ['Accept: application/json'],
        ]);
        $body = curl_exec($ch);
        $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($body === false || $body === '' || $code < 200 || $code >= 300) {
            return null;
        }
        return $body;
    }
    $ctx = stream_context_create([
        'http' => [
            'method' => 'GET',
            'timeout' => 3.0,
            'ignore_errors' => true,
            'header' => "Accept: application/json\r\n",
        ],
        'ssl' => [
            'verify_peer' => false,
            'verify_peer_name' => false,
        ],
    ]);
    $body = @file_get_contents($url, false, $ctx);
    return is_string($body) && $body !== '' ? $body : null;
}

function hardware_read_settings(): array {
    $slots = ops_max_channels();
    $detected = hardware_detect_devices();
    return [
        'slots' => $slots,
        'max_devices' => $slots,
        'max_channels' => $slots,
        'max_channel_id' => $slots - 1,
        'presets' => [2, 4, 8],
        'detected_devices' => $detected['count'],
        'detected_names' => $detected['names'],
        'detected_stale' => $detected['stale'],
    ];
}

/**
 * Parse nexvue-ops-status.sh output: "<active-state> <enabled-state>".
 * Tolerates the old single-token format (enabled falls back to "unknown").
 *
 * @return array{state: string, enabled: string}
 */
function parse_unit_status(string $stdout): array {
    $parts = preg_split('/\s+/', trim($stdout), -1, PREG_SPLIT_NO_EMPTY);
    if (!is_array($parts)) {
        $parts = [];
    }
    return [
        'state' => $parts[0] ?? 'unknown',
        'enabled' => $parts[1] ?? 'unknown',
    ];
}

function mediamtx_api_base(): string {
    $base = getenv('NEXVUE_MEDIAMTX_API_URL');
    if (!is_string($base) || $base === '') {
        $base = 'https://127.0.0.1:9997';
    }
    $base = rtrim($base, '/');
    $host = parse_url($base, PHP_URL_HOST);
    // Unverified TLS is only acceptable for loopback-to-self (same rule as
    // nexvue-metrics-server.py). Reject anything else rather than phone home.
    if (!is_string($host) || !in_array(strtolower($host), ['127.0.0.1', 'localhost', '::1'], true)) {
        fail(500, 'NEXVUE_MEDIAMTX_API_URL must be loopback');
    }
    return $base;
}

/** @return array{status: int, body: string} */
function mediamtx_http(string $method, string $urlPath): array {
    $url = mediamtx_api_base() . $urlPath;
    $ctx = stream_context_create([
        'http' => [
            'method' => $method,
            'timeout' => 5,
            'ignore_errors' => true,
            'header' => "Content-Length: 0\r\n",
        ],
        'ssl' => [
            'verify_peer' => false,
            'verify_peer_name' => false,
        ],
    ]);
    $bodyOut = @file_get_contents($url, false, $ctx);
    $status = 0;
    if (isset($http_response_header[0])
        && preg_match('/\s(\d{3})\s/', $http_response_header[0], $m)) {
        $status = (int)$m[1];
    }
    return ['status' => $status, 'body' => is_string($bodyOut) ? $bodyOut : ''];
}

function mediamtx_session_remote_ip(string $sessionId): string {
    $r = mediamtx_http('GET', '/v3/webrtcsessions/get/' . rawurlencode($sessionId));
    if ($r['status'] !== 200 || $r['body'] === '') {
        return '';
    }
    $data = json_decode($r['body'], true);
    if (!is_array($data)) {
        return '';
    }
    $addr = $data['remoteAddr'] ?? ($data['remote_addr'] ?? '');
    if (!is_string($addr)) {
        return '';
    }
    return kick_strip_ip_port($addr);
}

function logo_branding_dir(): string {
    $override = getenv('NEXVUE_BRANDING_DIR');
    if (is_string($override) && $override !== '') {
        return rtrim($override, '/');
    }
    return '/var/lib/nexvue/branding';
}

function logo_bin_path(): string {
    return logo_branding_dir() . '/logo.bin';
}

function logo_meta_path(): string {
    return logo_branding_dir() . '/logo.json';
}

/**
 * @return array{exists: bool, mime?: string, width?: int, height?: int, bytes?: int, mtime?: int, uploaded_at?: int}
 */
function logo_get_info(): array {
    $bin = logo_bin_path();
    $metaPath = logo_meta_path();
    if (!is_readable($bin) || !is_file($bin)) {
        return ['exists' => false];
    }
    $bytes = filesize($bin);
    if ($bytes === false || $bytes < 1) {
        return ['exists' => false];
    }
    $mtime = filemtime($bin);
    $info = [
        'exists' => true,
        'bytes' => (int)$bytes,
        'mtime' => is_int($mtime) ? $mtime : 0,
        'mime' => 'application/octet-stream',
        'width' => 0,
        'height' => 0,
        'uploaded_at' => is_int($mtime) ? $mtime : 0,
    ];
    if (is_readable($metaPath)) {
        $raw = @file_get_contents($metaPath);
        if (is_string($raw) && $raw !== '') {
            $meta = json_decode($raw, true);
            if (is_array($meta)) {
                if (isset($meta['mime']) && is_string($meta['mime'])) {
                    $info['mime'] = $meta['mime'];
                }
                if (isset($meta['width']) && is_numeric($meta['width'])) {
                    $info['width'] = (int)$meta['width'];
                }
                if (isset($meta['height']) && is_numeric($meta['height'])) {
                    $info['height'] = (int)$meta['height'];
                }
                if (isset($meta['bytes']) && is_numeric($meta['bytes'])) {
                    $info['bytes'] = (int)$meta['bytes'];
                }
                if (isset($meta['uploaded_at']) && is_numeric($meta['uploaded_at'])) {
                    $info['uploaded_at'] = (int)$meta['uploaded_at'];
                }
            }
        }
    }
    return $info;
}

/**
 * Decode base64 image payload, validate size/MIME, write logo.bin + logo.json atomically.
 *
 * @return array{mime: string, width: int, height: int, bytes: int, uploaded_at: int}
 */
function logo_put_base64(string $dataB64): array {
    $dataB64 = trim($dataB64);
    if ($dataB64 === '') {
        throw new InvalidArgumentException('data required');
    }
    // Strip optional data-URL prefix.
    if (str_starts_with($dataB64, 'data:')) {
        $comma = strpos($dataB64, ',');
        if ($comma === false) {
            throw new InvalidArgumentException('invalid data URL');
        }
        $dataB64 = substr($dataB64, $comma + 1);
    }
    $bin = base64_decode($dataB64, true);
    if ($bin === false || $bin === '') {
        throw new InvalidArgumentException('data must be base64 image bytes');
    }
    $len = strlen($bin);
    if ($len > LOGO_MAX_BYTES) {
        throw new InvalidArgumentException('logo exceeds 1 MB limit');
    }
    $img = @getimagesizefromstring($bin);
    if ($img === false || !isset($img['mime']) || !is_string($img['mime'])) {
        throw new InvalidArgumentException('unrecognized image data');
    }
    $mime = strtolower($img['mime']);
    if (!isset(LOGO_ALLOWED_MIMES[$mime])) {
        throw new InvalidArgumentException('logo must be PNG, JPEG, or WebP');
    }
    $width = (int)($img[0] ?? 0);
    $height = (int)($img[1] ?? 0);
    if ($width < 1 || $height < 1) {
        throw new InvalidArgumentException('invalid image dimensions');
    }

    $dir = logo_branding_dir();
    if (!is_dir($dir)) {
        if (!@mkdir($dir, 0750, true) && !is_dir($dir)) {
            throw new RuntimeException('cannot create branding directory');
        }
    }
    if (!is_writable($dir)) {
        throw new RuntimeException('branding directory not writable');
    }

    $uploadedAt = time();
    $meta = [
        'mime' => $mime,
        'width' => $width,
        'height' => $height,
        'bytes' => $len,
        'uploaded_at' => $uploadedAt,
    ];
    $metaJson = json_encode($meta, JSON_UNESCAPED_SLASHES);
    if ($metaJson === false) {
        throw new RuntimeException('failed to encode logo metadata');
    }

    $binPath = logo_bin_path();
    $metaPath = logo_meta_path();
    $tmpBin = $binPath . '.tmp.' . getmypid();
    $tmpMeta = $metaPath . '.tmp.' . getmypid();
    try {
        if (@file_put_contents($tmpBin, $bin) !== $len) {
            throw new RuntimeException('failed to write logo bytes');
        }
        if (@file_put_contents($tmpMeta, $metaJson) === false) {
            throw new RuntimeException('failed to write logo metadata');
        }
        if (!@rename($tmpBin, $binPath)) {
            throw new RuntimeException('failed to install logo bytes');
        }
        $tmpBin = '';
        if (!@rename($tmpMeta, $metaPath)) {
            throw new RuntimeException('failed to install logo metadata');
        }
        $tmpMeta = '';
    } finally {
        if ($tmpBin !== '' && is_file($tmpBin)) {
            @unlink($tmpBin);
        }
        if ($tmpMeta !== '' && is_file($tmpMeta)) {
            @unlink($tmpMeta);
        }
    }
    return $meta;
}

function logo_delete(): void {
    $bin = logo_bin_path();
    $meta = logo_meta_path();
    if (is_file($bin) && !@unlink($bin)) {
        throw new RuntimeException('failed to delete logo');
    }
    if (is_file($meta) && !@unlink($meta)) {
        throw new RuntimeException('failed to delete logo metadata');
    }
}

function network_mediamtx_yml_path(): string {
    $o = getenv('NEXVUE_MEDIAMTX_YML');
    if (is_string($o) && $o !== '') {
        return $o;
    }
    return '/etc/nexvue/mediamtx.yml';
}

function network_sanitize_hostname(string $raw): string {
    $v = strtolower(trim($raw));
    if ($v === '') {
        return '';
    }
    if (str_contains($v, '://') || str_contains($v, '/') || str_contains($v, ':')) {
        throw new InvalidArgumentException('Enter a hostname like nexvue.example.com');
    }
    if (filter_var($v, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) !== false) {
        throw new InvalidArgumentException('Put IP addresses in Public IP, not hostname');
    }
    if (strlen($v) > 253 || !preg_match('/^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/', $v)) {
        throw new InvalidArgumentException('Enter a hostname like nexvue.example.com');
    }
    return $v;
}

function network_sanitize_ip(string $raw): string {
    $v = trim($raw);
    if ($v === '') {
        return '';
    }
    if (filter_var($v, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) === false) {
        throw new InvalidArgumentException('Enter an IPv4 address like 203.0.113.40');
    }
    $parts = explode('.', $v);
    $a = (int)$parts[0];
    $b = (int)($parts[1] ?? 0);
    if ($v === '0.0.0.0' || $v === '255.255.255.255') {
        throw new InvalidArgumentException('Enter a reachable IPv4 address');
    }
    if ($a === 127) {
        throw new InvalidArgumentException('Loopback addresses cannot be used as a public IP');
    }
    if ($a === 169 && $b === 254) {
        throw new InvalidArgumentException('Link-local addresses cannot be used as a public IP');
    }
    if ($a >= 224) {
        throw new InvalidArgumentException('Multicast addresses cannot be used as a public IP');
    }
    return $v;
}

/**
 * First hostname + first IPv4 from webrtcAdditionalHosts (flow or block list).
 *
 * @return array{hostname: string, ip: string}
 */
function network_parse_additional_hosts(string $yml): array {
    $out = ['hostname' => '', 'ip' => ''];
    $tokens = [];
    if (preg_match('/^webrtcAdditionalHosts:\s*\[(.*?)\]\s*$/m', $yml, $m)) {
        $parts = preg_split('/\s*,\s*|\s+/', trim($m[1])) ?: [];
        foreach ($parts as $part) {
            $tok = trim(trim((string)$part), "\"'");
            if ($tok !== '') {
                $tokens[] = $tok;
            }
        }
    } elseif (preg_match('/^webrtcAdditionalHosts:\s*\n((?:[ \t]+-[ \t]*.+\n?)*)/m', $yml, $m)) {
        if (preg_match_all('/^[ \t]+-[ \t]*(.+)$/m', $m[1], $mm)) {
            foreach ($mm[1] as $part) {
                $tok = trim(trim((string)$part), "\"'");
                if ($tok !== '') {
                    $tokens[] = $tok;
                }
            }
        }
    }
    foreach ($tokens as $tok) {
        if (filter_var($tok, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) !== false) {
            if ($out['ip'] === '') {
                $out['ip'] = $tok;
            }
        } elseif ($out['hostname'] === '') {
            $out['hostname'] = strtolower($tok);
        }
    }
    return $out;
}

/**
 * Station public hostname / IP. Prefers nexvue.env; if both keys are empty,
 * falls back to MediaMTX webrtcAdditionalHosts so an existing hand-edit shows.
 *
 * @return array{hostname: string, ip: string}
 */
function network_read_settings(): array {
    $hostname = auth_read_env_key('NEXVUE_PUBLIC_HOSTNAME');
    $ip = auth_read_env_key('NEXVUE_PUBLIC_IP');
    if ($hostname !== '' || $ip !== '') {
        return ['hostname' => $hostname, 'ip' => $ip];
    }
    $path = network_mediamtx_yml_path();
    if (is_readable($path)) {
        $raw = @file_get_contents($path);
        if (is_string($raw) && $raw !== '') {
            return network_parse_additional_hosts($raw);
        }
    }
    return ['hostname' => '', 'ip' => ''];
}

function network_is_rfc1918(string $ip): bool {
    $parts = explode('.', $ip);
    if (count($parts) !== 4) {
        return false;
    }
    $a = (int)$parts[0];
    $b = (int)$parts[1];
    if ($a === 10) {
        return true;
    }
    if ($a === 192 && $b === 168) {
        return true;
    }
    if ($a === 172 && $b >= 16 && $b <= 31) {
        return true;
    }
    return false;
}

/**
 * @return list<string>
 */
function network_dns_a(string $hostname): array {
    $stub = getenv('NEXVUE_NETWORK_TEST_DNS_STUB');
    if (is_string($stub) && $stub !== '') {
        $map = json_decode($stub, true);
        if (!is_array($map)) {
            return [];
        }
        $raw = $map[$hostname] ?? [];
        if (!is_array($raw)) {
            return [];
        }
        $out = [];
        foreach ($raw as $ip) {
            if (is_string($ip) && filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) !== false) {
                $out[] = $ip;
            }
        }
        return array_values(array_unique($out));
    }
    $recs = @dns_get_record($hostname, DNS_A);
    if (!is_array($recs)) {
        return [];
    }
    $out = [];
    foreach ($recs as $rec) {
        $ip = isset($rec['ip']) && is_string($rec['ip']) ? $rec['ip'] : '';
        if ($ip !== '' && filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) !== false) {
            $out[] = $ip;
        }
    }
    return array_values(array_unique($out));
}

function network_extract_ipv4(string $body): string {
    $text = trim(str_replace("\r\n", "\n", $body));
    if ($text === '') {
        return '';
    }
    if (preg_match('/^ip=(\d{1,3}(?:\.\d{1,3}){3})\s*$/m', $text, $m)) {
        $ip = $m[1];
        return filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) !== false ? $ip : '';
    }
    $line = trim(explode("\n", $text, 2)[0]);
    if (filter_var($line, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) !== false) {
        return $line;
    }
    return '';
}

function network_http_get(string $url, float $timeout = 3.0): string {
    $scheme = parse_url($url, PHP_URL_SCHEME);
    if (!in_array($scheme, ['https', 'http'], true)) {
        return '';
    }
    $ctx = stream_context_create([
        'http' => [
            'method' => 'GET',
            'timeout' => $timeout,
            'ignore_errors' => true,
            'header' => "User-Agent: NexVUE-network-test\r\n",
        ],
        'ssl' => [
            'verify_peer' => true,
            'verify_peer_name' => true,
        ],
    ]);
    $out = @file_get_contents($url, false, $ctx);
    return is_string($out) ? $out : '';
}

function network_wan_ipv4(): string {
    $stub = getenv('NEXVUE_NETWORK_TEST_WAN_STUB');
    if (is_string($stub) && $stub !== '') {
        return network_extract_ipv4($stub);
    }
    if (getenv('NEXVUE_NETWORK_TEST_WAN_FAIL') === '1') {
        return '';
    }
    $urls = [
        'https://cloudflare.com/cdn-cgi/trace',
        'https://1.1.1.1/cdn-cgi/trace',
        'https://api.ipify.org',
    ];
    foreach ($urls as $url) {
        $ip = network_extract_ipv4(network_http_get($url, 3.0));
        if ($ip !== '') {
            return $ip;
        }
    }
    return '';
}

function network_tcp_open(string $host, int $port, float $timeout = 2.0): bool {
    if ($host === '' || ($port !== 443 && $port !== 8889)) {
        return false;
    }
    $stub = getenv('NEXVUE_NETWORK_TEST_TCP_STUB');
    if (is_string($stub) && $stub !== '') {
        $map = json_decode($stub, true);
        if (!is_array($map)) {
            return false;
        }
        return !empty($map[$host . ':' . $port]);
    }
    $target = 'tcp://' . $host . ':' . $port;
    $errno = 0;
    $errstr = '';
    $fp = @stream_socket_client($target, $errno, $errstr, $timeout);
    if (is_resource($fp)) {
        fclose($fp);
        return true;
    }
    return false;
}

/**
 * Advisory reachability probe. Never required to Save.
 *
 * @return array{
 *   hostname: string,
 *   ip: string,
 *   status: string,
 *   summary: string,
 *   checks: list<array{id:string,label:string,status:string,detail:string}>
 * }
 */
function network_probe(string $hostname, string $ip): array {
    $checks = [];
    $dnsIps = [];
    if ($hostname !== '') {
        $dnsIps = network_dns_a($hostname);
        if ($dnsIps === []) {
            $checks[] = [
                'id' => 'dns',
                'label' => 'DNS',
                'status' => 'err',
                'detail' => $hostname . ' did not resolve to an IPv4 address',
            ];
        } else {
            $listed = implode(', ', $dnsIps);
            $status = 'ok';
            $detail = $hostname . ' → ' . $listed;
            if ($ip !== '' && !in_array($ip, $dnsIps, true)) {
                $status = 'warn';
                $detail .= ' (does not match Public IP ' . $ip . ')';
            } elseif ($ip !== '') {
                $detail .= ' (matches Public IP)';
            }
            $checks[] = [
                'id' => 'dns',
                'label' => 'DNS',
                'status' => $status,
                'detail' => $detail,
            ];
        }
    } else {
        $checks[] = [
            'id' => 'dns',
            'label' => 'DNS',
            'status' => 'skip',
            'detail' => 'No Public hostname — skipped',
        ];
    }

    $wan = network_wan_ipv4();
    if ($wan === '') {
        $checks[] = [
            'id' => 'wan',
            'label' => 'WAN IP',
            'status' => 'warn',
            'detail' => 'Could not learn this box\'s WAN IPv4 (outbound check failed)',
        ];
    } elseif ($ip !== '' && network_is_rfc1918($ip)) {
        $checks[] = [
            'id' => 'wan',
            'label' => 'WAN IP',
            'status' => 'ok',
            'detail' => 'Internet sees ' . $wan . ' (Public IP ' . $ip . ' is private / NAT — expected)',
        ];
    } elseif ($ip !== '' && $wan === $ip) {
        $checks[] = [
            'id' => 'wan',
            'label' => 'WAN IP',
            'status' => 'ok',
            'detail' => $wan . ' matches Public IP',
        ];
    } elseif ($ip !== '') {
        $checks[] = [
            'id' => 'wan',
            'label' => 'WAN IP',
            'status' => 'warn',
            'detail' => 'Internet sees ' . $wan . ' (does not match Public IP ' . $ip . ')',
        ];
    } elseif ($dnsIps !== [] && in_array($wan, $dnsIps, true)) {
        $checks[] = [
            'id' => 'wan',
            'label' => 'WAN IP',
            'status' => 'ok',
            'detail' => $wan . ' matches DNS',
        ];
    } elseif ($dnsIps !== []) {
        $checks[] = [
            'id' => 'wan',
            'label' => 'WAN IP',
            'status' => 'warn',
            'detail' => 'Internet sees ' . $wan . ' (not in DNS ' . implode(', ', $dnsIps) . ')',
        ];
    } else {
        $checks[] = [
            'id' => 'wan',
            'label' => 'WAN IP',
            'status' => 'ok',
            'detail' => 'Internet sees ' . $wan,
        ];
    }

    $tcpHost = $hostname !== '' ? $hostname : $ip;
    foreach ([443, 8889] as $port) {
        $id = 'tcp_' . $port;
        $label = 'TCP ' . $port;
        if ($tcpHost === '') {
            $checks[] = [
                'id' => $id,
                'label' => $label,
                'status' => 'skip',
                'detail' => 'No hostname or IP — skipped',
            ];
            continue;
        }
        if (network_tcp_open($tcpHost, $port)) {
            $checks[] = [
                'id' => $id,
                'label' => $label,
                'status' => 'ok',
                'detail' => $tcpHost . ':' . $port . ' accepted a connection',
            ];
        } else {
            $checks[] = [
                'id' => $id,
                'label' => $label,
                'status' => 'warn',
                'detail' => $tcpHost . ':' . $port . ' did not accept from this box (hairpin NAT is common — try off-site)',
            ];
        }
    }

    $status = 'ok';
    foreach ($checks as $c) {
        if ($c['status'] === 'err') {
            $status = 'err';
            break;
        }
        if ($c['status'] === 'warn' && $status === 'ok') {
            $status = 'warn';
        }
    }
    $summary = 'Looks good from this box. Save is still required to apply. Off-site WHEP is the real media test.';
    if ($status === 'err') {
        $summary = 'DNS failed. Fix the name (or leave it blank) before expecting Let\'s Encrypt or off-site viewers.';
    } elseif ($status === 'warn') {
        $summary = 'Some checks look off. Save is still allowed — port probes from this box can fail even when off-site works.';
    }
    return [
        'hostname' => $hostname,
        'ip' => $ip,
        'status' => $status,
        'summary' => $summary,
        'checks' => $checks,
    ];
}

/**
 * Parse AUDIO_EMBEDS (comma list of 1–8). Blank / all → [1..8].
 *
 * @return list<int>
 */
function parse_audio_embeds(?string $raw): array {
    $raw = strtolower(trim((string)$raw));
    if ($raw === '' || $raw === '1-8' || $raw === 'all' || $raw === '*') {
        return [1, 2, 3, 4, 5, 6, 7, 8];
    }
    $out = [];
    $seen = [];
    foreach (explode(',', $raw) as $part) {
        $part = trim($part);
        if ($part === '' || !ctype_digit($part)) {
            continue;
        }
        $n = (int)$part;
        if ($n < 1 || $n > 8 || isset($seen[$n])) {
            continue;
        }
        $seen[$n] = true;
        $out[] = $n;
    }
    if ($out === []) {
        return [1, 2, 3, 4, 5, 6, 7, 8];
    }
    sort($out);
    return $out;
}

/**
 * Suggest AUDIO_LAYOUT role presets + AUDIO_EMBEDS from active embed indices.
 * Encode is always 8ch — suggestions only set player/Settings metadata.
 * Energy probe only — operator must confirm before writing .env.
 *
 * @param list<int> $activeMask
 * @return list<array{layout:string,label:string,score:int,exact:bool,embeds:list<int>,embeds_csv:string}>
 */
function audio_probe_suggest(array $activeMask): array {
    $active = [];
    foreach ($activeMask as $i) {
        $n = (int)$i;
        if ($n >= 1 && $n <= 8) {
            $active[$n] = true;
        }
    }
    if ($active === []) {
        return [];
    }
    $defs = [
        [
            'layout' => '51_sap',
            'embeds' => [1, 2, 3, 4, 5, 6, 7, 8],
            'label' => '5.1 + SAP (embeds 1–8)',
        ],
        [
            'layout' => '51',
            'embeds' => [1, 2, 3, 4, 5, 6],
            'label' => '5.1 (embeds 1–6)',
        ],
        [
            'layout' => 'stereo_sap',
            'embeds' => [1, 2, 7, 8],
            'label' => 'stereo + SAP (1–2 + 7–8)',
        ],
        [
            'layout' => 'stereo',
            'embeds' => [1, 2],
            'label' => 'stereo (embeds 1–2)',
        ],
    ];
    $out = [];
    foreach ($defs as $d) {
        $need = $d['embeds'];
        $missing = 0;
        foreach ($need as $e) {
            if (!isset($active[$e])) {
                $missing++;
            }
        }
        if ($missing === count($need)) {
            continue;
        }
        $extra = 0;
        foreach (array_keys($active) as $e) {
            if (!in_array($e, $need, true)) {
                $extra++;
            }
        }
        $score = 100 - (25 * $missing) - (8 * $extra);
        if ($score < 1) {
            continue;
        }
        // Prefer enabling every embed the probe heard (plus the preset's set).
        $enable = array_values(array_unique(array_merge($need, array_keys($active))));
        sort($enable);
        $out[] = [
            'layout' => $d['layout'],
            'label' => $d['label'],
            'score' => $score,
            'exact' => ($missing === 0 && $extra === 0),
            'embeds' => $enable,
            'embeds_csv' => implode(',', $enable),
        ];
    }
    usort($out, static function (array $a, array $b): int {
        return $b['score'] <=> $a['score'];
    });
    return $out;
}

// Library mode for unit tests (php -r 'include …' without NEXVUE_OPS_HTTP).
if (PHP_SAPI === 'cli' && getenv('NEXVUE_OPS_HTTP') === false) {
    return;
}

function read_json_body(): array {
    $raw = file_get_contents('php://input');
    if ($raw === false || $raw === '') {
        return [];
    }
    $data = json_decode($raw, true);
    if (!is_array($data)) {
        fail(400, 'request body must be JSON object');
    }
    return $data;
}

function sudo_run(array $argv, ?string $stdin = null): array {
    // $argv[0] is the wrapper basename under /usr/local/bin/
    $cmd = [SUDO, '-n'];
    foreach ($argv as $a) {
        $cmd[] = $a;
    }
    $descriptors = [
        0 => ['pipe', 'r'],
        1 => ['pipe', 'w'],
        2 => ['pipe', 'w'],
    ];
    $proc = proc_open($cmd, $descriptors, $pipes, null, null, ['bypass_shell' => true]);
    if (!is_resource($proc)) {
        fail(500, 'failed to start privileged helper');
    }
    if ($stdin !== null) {
        fwrite($pipes[0], $stdin);
    }
    fclose($pipes[0]);
    $stdout = stream_get_contents($pipes[1]);
    $stderr = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $code = proc_close($proc);
    return ['code' => $code, 'stdout' => (string)$stdout, 'stderr' => (string)$stderr];
}

function unit_allowed(string $unit): bool {
    return (bool)preg_match('/^(mediamtx|nexvue-status|nexvue-metrics|nexvue-encode@[0-7])$/', $unit);
}

function list_channel_ids(): array {
    $ids = [];
    $max = ops_max_channel_id();
    for ($i = 0; $i <= $max; $i++) {
        if (is_readable(CHANNELS_DIR . "/{$i}.env")) {
            $ids[] = $i;
        }
    }
    return $ids;
}

function action_from_request(array $body): string {
    $action = $_GET['action'] ?? ($body['action'] ?? '');
    if (!is_string($action) || $action === '') {
        fail(400, 'action required');
    }
    return $action;
}

/**
 * Role gate for ops actions. Share sessions may only call aliases + kick_check.
 */
function ops_require_auth(string $action): void {
    if (auth_bypass_enabled()) {
        return;
    }
    $any = ['aliases', 'kick_check'];
    $hot = in_array($action, $any, true);
    // Player hot paths: skip migrate (session cache) so status/WHEP stay snappy.
    if (!$hot) {
        try {
            auth_migrate();
        } catch (Throwable $e) {
            fail(500, 'auth store unavailable');
        }
    }
    $adminOnly = [
        'services', 'journal', 'journal_clear', 'set_enabled', 'set_running',
        'support_bundle', 'update_status', 'update_repo', 'update_setup_log',
        'reboot_host',
        'network_get', 'network_put', 'network_test',
        'hardware_get', 'hardware_put',
        'tls_status', 'tls_issue', 'tls_upload',
        'turn_get', 'turn_put', 'turn_test',
        'sfu_get', 'sfu_put', 'sfu_test',
    ];
    try {
        if ($hot) {
            auth_require_any();
            auth_session_release();
            return;
        }
        if (in_array($action, $adminOnly, true)) {
            auth_require_roles(['admin']);
            auth_session_release();
            return;
        }
        auth_require_roles(['admin', 'operator']);
        auth_session_release();
    } catch (RuntimeException $e) {
        auth_session_release();
        $msg = $e->getMessage();
        if ($msg === 'unauthorized') {
            fail(401, 'unauthorized');
        }
        fail(403, 'forbidden');
    }
}

/**
 * Parse a channel .env into a KEY=>value map (no sudo). Returns null if unreadable.
 * @return array<string,string>|null
 */
function read_channel_env_map(int $id): ?array {
    $path = CHANNELS_DIR . "/{$id}.env";
    if (!is_readable($path)) {
        return null;
    }
    $raw = @file_get_contents($path);
    if (!is_string($raw)) {
        return null;
    }
    $keys = [];
    foreach (preg_split("/\r\n|\n|\r/", $raw) ?: [] as $line) {
        $line = trim($line);
        if ($line === '' || str_starts_with($line, '#')) {
            continue;
        }
        if (!str_contains($line, '=')) {
            continue;
        }
        [$k, $v] = explode('=', $line, 2);
        $k = trim($k);
        if ($k === '' || !preg_match('/^[A-Za-z_][A-Za-z0-9_]*$/', $k)) {
            continue;
        }
        $v = trim($v);
        if (
            (str_starts_with($v, '"') && str_ends_with($v, '"'))
            || (str_starts_with($v, "'") && str_ends_with($v, "'"))
        ) {
            $v = substr($v, 1, -1);
        }
        // Strip unquoted trailing comment.
        if (!str_contains($v, '"') && !str_contains($v, "'") && str_contains($v, '#')) {
            $v = trim(explode('#', $v, 2)[0]);
        }
        $keys[$k] = $v;
    }
    return $keys;
}

$body = read_json_body();
$action = action_from_request($body);
ops_require_auth($action);

// ---- support_bundle (binary zip — before JSON Content-Type) -------------------

if ($action === 'support_bundle') {
    // Journals for 72h can exceed PHP's default 30s.
    if (function_exists('set_time_limit')) {
        @set_time_limit(180);
    }
    $hours = (int)($body['hours'] ?? ($_GET['hours'] ?? 24));
    $allowed = [1, 6, 12, 24, 48, 72];
    if (!in_array($hours, $allowed, true)) {
        fail(400, 'hours must be one of: 1, 6, 12, 24, 48, 72');
    }
    $ip = $_SERVER['REMOTE_ADDR'] ?? '';
    if (!is_string($ip) || $ip === '') {
        $ip = 'unknown';
    }
    $ip = substr(preg_replace('/[^0-9a-fA-F.:]/', '', $ip) ?? '', 0, 64);
    $argv = ['/usr/local/bin/nexvue-ops-support-bundle.sh', (string)$hours];
    if ($ip !== '') {
        $argv[] = $ip;
    }
    $r = sudo_run($argv);
    if ($r['code'] !== 0) {
        fail(
            500,
            trim($r['stderr']) !== ''
                ? trim($r['stderr'])
                : 'support bundle failed (is nexvue-ops-support-bundle.sh installed?)'
        );
    }
    $lines = preg_split("/\r\n|\n|\r/", trim($r['stdout'])) ?: [];
    $path = '';
    foreach (array_reverse($lines) as $line) {
        $line = trim($line);
        if ($line !== '' && str_starts_with($line, '/') && str_ends_with($line, '.zip')) {
            $path = $line;
            break;
        }
    }
    if ($path === '' || !is_file($path)) {
        fail(500, 'support bundle produced no zip path');
    }
    $real = realpath($path);
    $supportRoot = realpath('/var/lib/nexvue/support');
    if ($real === false || $supportRoot === false || !str_starts_with($real, $supportRoot . DIRECTORY_SEPARATOR)) {
        fail(500, 'refusing to serve zip outside /var/lib/nexvue/support');
    }
    $basename = basename($real);
    if (!preg_match('/^nexvue-support-[A-Za-z0-9._-]+\.zip$/', $basename)) {
        fail(500, 'unexpected bundle filename');
    }
    header('Content-Type: application/zip');
    header('Content-Disposition: attachment; filename="' . $basename . '"');
    header('Content-Length: ' . (string)filesize($real));
    header('Cache-Control: no-store');
    header('X-Content-Type-Options: nosniff');
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    readfile($real);
    exit;
}

// ---- update_status / update_repo (JSON from nexvue-ops-update.sh) ------------

/**
 * Parse helper JSON from stdout (or a trailing JSON line after setup banners).
 * @return array<string,mixed>|null
 */
function ops_parse_helper_json(string $raw): ?array {
    $raw = trim($raw);
    if ($raw === '') {
        return null;
    }
    $parsed = json_decode($raw, true);
    if (is_array($parsed)) {
        return $parsed;
    }
    // Prefer the last line that looks like a JSON object (setup.sh used to
    // leak banners onto stdout before the final {"ok":...}).
    $lines = preg_split("/\r\n|\n|\r/", $raw) ?: [];
    for ($i = count($lines) - 1; $i >= 0; $i--) {
        $line = trim($lines[$i]);
        if ($line === '' || !str_starts_with($line, '{')) {
            continue;
        }
        $parsed = json_decode($line, true);
        if (is_array($parsed)) {
            return $parsed;
        }
    }
    return null;
}

if ($action === 'update_status' || $action === 'update_repo') {
    if (function_exists('set_time_limit')) {
        @set_time_limit($action === 'update_repo' ? 600 : 120);
    }
    $helper = '/usr/local/bin/nexvue-ops-update.sh';
    if (!is_file($helper)) {
        fail(
            500,
            'nexvue-ops-update.sh not installed — on the edge host: '
            . 'cd "$(cat /etc/nexvue/repo.path 2>/dev/null || echo /path/to/NexVUE)" && sudo ./setup.sh'
        );
    }
    if (!is_executable($helper)) {
        fail(500, 'nexvue-ops-update.sh is not executable — re-run sudo ./setup.sh');
    }
    $verb = $action === 'update_repo' ? 'apply' : 'status';
    $r = sudo_run([$helper, $verb]);
    $parsed = ops_parse_helper_json($r['stdout']);
    if (!is_array($parsed)) {
        $errRaw = trim($r['stderr']);
        $errParsed = ops_parse_helper_json($errRaw);
        if (is_array($errParsed) && isset($errParsed['error'])) {
            fail(500, (string)$errParsed['error']);
        }
        if ($errRaw !== '') {
            // Common: sudoers missing the update line, or sudo -n denied.
            if (stripos($errRaw, 'not allowed') !== false
                || stripos($errRaw, 'password') !== false) {
                fail(
                    500,
                    'sudo denied for nexvue-ops-update.sh — install sudoers: '
                    . 'sudo install -m 440 nexvue-ops.sudoers /etc/sudoers.d/nexvue-ops '
                    . '&& sudo visudo -cf /etc/sudoers.d/nexvue-ops'
                );
            }
            fail(500, $errRaw);
        }
        fail(
            500,
            'update helper returned no JSON (exit ' . (int)$r['code'] . ') — '
            . 're-run sudo ./setup.sh from the clone to install nexvue-ops-update.sh + sudoers'
        );
    }
    $parsed = ops_merge_setup_log_fields($parsed);
    if ($r['code'] !== 0 || empty($parsed['ok'])) {
        if (!headers_sent()) {
            header('Content-Type: application/json');
            header('Cache-Control: no-store');
        }
        http_response_code(500);
        $parsed['ok'] = false;
        if (!isset($parsed['error']) || $parsed['error'] === '') {
            $parsed['error'] = 'update helper failed';
        }
        echo json_encode($parsed, JSON_UNESCAPED_SLASHES);
        exit;
    }
    if (!headers_sent()) {
        header('Content-Type: application/json');
        header('Cache-Control: no-store');
    }
    echo json_encode($parsed, JSON_UNESCAPED_SLASHES);
    exit;
}

if ($action === 'update_setup_log') {
    $st = ops_read_setup_state();
    $blob = ops_setup_log_text();
    if ($blob['log'] === '' && !is_readable(ops_setup_log_path())) {
        fail(404, 'no setup.sh log yet — run Update from repo or sudo ./setup.sh');
    }
    if (!headers_sent()) {
        header('Content-Type: application/json');
        header('Cache-Control: no-store');
    }
    echo json_encode([
        'ok' => true,
        'last_setup_ok' => $st['ok'],
        'last_setup_at' => $st['at'],
        'truncated' => $blob['truncated'],
        'log' => $blob['log'],
    ], JSON_UNESCAPED_SLASHES);
    exit;
}

if ($action === 'reboot_host') {
    $helper = '/usr/local/bin/nexvue-ops-reboot.sh';
    if (!is_file($helper)) {
        fail(
            500,
            'nexvue-ops-reboot.sh not installed — on the edge host: '
            . 'cd "$(cat /etc/nexvue/repo.path 2>/dev/null || echo /path/to/NexVUE)" && sudo ./setup.sh'
        );
    }
    if (!is_executable($helper)) {
        fail(500, 'nexvue-ops-reboot.sh is not executable — re-run sudo ./setup.sh');
    }
    $r = sudo_run([$helper]);
    if ($r['code'] !== 0) {
        $err = trim($r['stderr']);
        if (stripos($err, 'not allowed') !== false
            || stripos($err, 'password') !== false) {
            fail(
                500,
                'sudo denied for nexvue-ops-reboot.sh — install sudoers: '
                . 'sudo install -m 440 nexvue-ops.sudoers /etc/sudoers.d/nexvue-ops '
                . '&& sudo visudo -cf /etc/sudoers.d/nexvue-ops'
            );
        }
        fail(500, $err !== '' ? $err : 'reboot failed');
    }
    if (!headers_sent()) {
        header('Content-Type: application/json');
        header('Cache-Control: no-store');
    }
    echo json_encode(['ok' => true, 'rebooting' => true], JSON_UNESCAPED_SLASHES);
    exit;
}

header('Content-Type: application/json');
header('Cache-Control: no-store');

// ---- services -----------------------------------------------------------------

if ($action === 'services') {
    $core = ['mediamtx', 'nexvue-status', 'nexvue-metrics'];
    $units = $core;
    foreach (list_channel_ids() as $id) {
        $units[] = "nexvue-encode@{$id}";
    }
    // Always show encode@0-7 slots that have env files; if none, still show core.
    $items = [];
    foreach ($units as $unit) {
        $r = sudo_run(['/usr/local/bin/nexvue-ops-status.sh', $unit]);
        $st = parse_unit_status($r['stdout']);
        $items[] = [
            'unit' => $unit,
            'state' => $st['state'],
            'enabled' => $st['enabled'],
            'can_toggle' => unit_enable_allowed($unit),
            'ok' => ($st['state'] === 'active'),
        ];
    }
    echo json_encode(['ok' => true, 'services' => $items]);
    exit;
}

// ---- journal ------------------------------------------------------------------

if ($action === 'journal') {
    $unit = $body['unit'] ?? ($_GET['unit'] ?? '');
    $lines = (int)($body['lines'] ?? ($_GET['lines'] ?? 100));
    $since = $body['since'] ?? ($_GET['since'] ?? '');
    if (!is_string($unit) || !unit_allowed($unit)) {
        fail(400, 'invalid unit');
    }
    if ($lines < 1) {
        $lines = 1;
    }
    if ($lines > 500) {
        $lines = 500;
    }
    $argv = ['/usr/local/bin/nexvue-ops-journal.sh', $unit, (string)$lines];
    if (is_string($since) && $since !== '') {
        $argv[] = $since;
    }
    $r = sudo_run($argv);
    if ($r['code'] !== 0) {
        fail(500, trim($r['stderr']) !== '' ? trim($r['stderr']) : 'journalctl failed');
    }
    echo json_encode(['ok' => true, 'unit' => $unit, 'log' => $r['stdout']]);
    exit;
}

// ---- journal_clear (selected unit only; watermark, not host vacuum) ----------

if ($action === 'journal_clear') {
    $unit = $body['unit'] ?? ($_GET['unit'] ?? '');
    if (!is_string($unit) || !unit_allowed($unit)) {
        fail(400, 'invalid unit');
    }
    $r = sudo_run([
        '/usr/local/bin/nexvue-ops-journal.sh',
        'clear',
        $unit,
    ]);
    if ($r['code'] !== 0) {
        fail(500, trim($r['stderr']) !== '' ? trim($r['stderr']) : 'journal clear failed');
    }
    echo json_encode([
        'ok' => true,
        'unit' => $unit,
        'output' => trim($r['stdout'] . "\n" . $r['stderr']),
    ]);
    exit;
}

// ---- audio_probe (DeckLink embedded PCM energy → layout suggestions) ---------

if ($action === 'audio_probe') {
    $id = $body['id'] ?? ($_GET['id'] ?? null);
    if (!channel_id_ok($id)) {
        fail(400, 'id must be 0-' . ops_max_channel_id());
    }
    $id = (int)$id;
    $durationMs = (int)($body['duration_ms'] ?? ($_GET['duration_ms'] ?? 1000));
    if ($durationMs < 200) {
        $durationMs = 200;
    }
    if ($durationMs > 3000) {
        $durationMs = 3000;
    }

    $envR = sudo_run(['/usr/local/bin/nexvue-ops-env-read.sh', (string)$id]);
    if ($envR['code'] !== 0) {
        fail(404, trim($envR['stderr']) !== '' ? trim($envR['stderr']) : 'channel env not found');
    }
    $env = json_decode($envR['stdout'], true);
    if (!is_array($env)) {
        fail(500, 'invalid channel env JSON');
    }
    $keys = $env['keys'] ?? $env;
    if (!is_array($keys)) {
        fail(500, 'invalid channel env keys');
    }
    $inputType = strtolower(trim((string)($keys['INPUT_TYPE'] ?? 'decklink')));
    if ($inputType === '') {
        $inputType = 'decklink';
    }
    if ($inputType !== 'decklink') {
        fail(400, 'audio probe is DeckLink-only (INPUT_TYPE=decklink)');
    }
    $devRaw = trim((string)($keys['DEVICE_NUMBER'] ?? ''));
    if ($devRaw === '' || !ctype_digit($devRaw)) {
        fail(400, 'DEVICE_NUMBER missing or invalid');
    }
    $device = (int)$devRaw;
    if ($device < 0 || $device > 15) {
        fail(400, 'DEVICE_NUMBER out of range');
    }

    $unit = "nexvue-encode@{$id}";
    $st = parse_unit_status(sudo_run(['/usr/local/bin/nexvue-ops-status.sh', $unit])['stdout']);
    $wasRunning = ($st['state'] === 'active' || $st['state'] === 'activating');
    $stopped = false;
    if ($wasRunning) {
        $stopR = sudo_run(['/usr/local/bin/nexvue-ops-enable.sh', 'stop', $unit]);
        if ($stopR['code'] !== 0) {
            fail(500, trim($stopR['stderr']) !== '' ? trim($stopR['stderr']) : 'failed to stop encoder for probe');
        }
        $stopped = true;
        // Brief settle so DeckLink releases the exclusive open.
        usleep(400000);
    }

    $probeArgv = ['/usr/local/bin/nexvue-ops-audio-probe.sh', (string)$device, (string)$durationMs];
    $r = sudo_run($probeArgv);

    if ($stopped) {
        $startR = sudo_run(['/usr/local/bin/nexvue-ops-enable.sh', 'start', $unit]);
        if ($startR['code'] !== 0) {
            fail(500, 'probe ran but failed to restart ' . $unit . ': ' .
                (trim($startR['stderr']) !== '' ? trim($startR['stderr']) : 'start failed'));
        }
    }

    $probe = json_decode(trim($r['stdout']), true);
    if (!is_array($probe)) {
        $err = trim($r['stderr']);
        fail(500, $err !== '' ? $err : 'audio probe returned invalid JSON');
    }
    if (($probe['ok'] ?? false) !== true) {
        $msg = (string)($probe['error'] ?? 'probe failed');
        if (!empty($probe['busy'])) {
            $msg = 'DeckLink device busy (stop other capture on this connector and retry)';
        }
        if ($msg === 'probe_not_installed') {
            $msg = 'decklink-audio-probe not installed — run: make && sudo make install';
        }
        fail(500, $msg);
    }

    $mask = [];
    if (isset($probe['active_mask']) && is_array($probe['active_mask'])) {
        foreach ($probe['active_mask'] as $m) {
            $mask[] = (int)$m;
        }
    }
    $suggestions = audio_probe_suggest($mask);
    $probe['suggestions'] = $suggestions;
    $probe['channel_id'] = $id;
    $probe['encoder_stopped'] = $stopped;
    $probe['ok'] = true;
    echo json_encode($probe);
    exit;
}

// ---- aliases (lightweight for player/multiview) -------------------------------

if ($action === 'aliases') {
    $aliases = [];
    $devices = [];
    $audioChannels = [];
    $audioLayouts = [];
    $audioEmbeds = [];
    foreach (list_channel_ids() as $id) {
        // Prefer direct read (no sudo). setup.sh makes channel .env world-readable.
        $keys = read_channel_env_map($id);
        if ($keys === null) {
            $r = sudo_run(['/usr/local/bin/nexvue-ops-env-read.sh', (string)$id]);
            if ($r['code'] !== 0) {
                continue;
            }
            $data = json_decode($r['stdout'], true);
            if (!is_array($data) || empty($data['ok'])) {
                continue;
            }
            $keys = $data['keys'] ?? [];
            if (!is_array($keys)) {
                continue;
            }
        }
        $path = $keys['CHANNEL_PATH'] ?? ("ch{$id}");
        $alias = $keys['CHANNEL_ALIAS'] ?? '';
        $label = $alias !== '' ? $alias : $path;
        $aliases[$path] = $label;
        $aliases[(string)$id] = $label;
        $dev = $keys['DEVICE_NUMBER'] ?? (string)$id;
        $devices[$path] = is_numeric($dev) ? (int)$dev : $id;
        // AUDIO_LAYOUT is a player role preset only — encode is always 8ch Opus.
        $layout = isset($keys['AUDIO_LAYOUT']) ? strtolower(trim((string)$keys['AUDIO_LAYOUT'])) : '';
        $layout = str_replace(['-', '.'], ['_', ''], $layout);
        if ($layout === '5_1' || $layout === '51' || $layout === 'surround') {
            $layout = '51';
        } elseif ($layout === '5_1_sap' || $layout === '51_sap' || $layout === 'surround_sap') {
            $layout = '51_sap';
        } elseif ($layout === 'sap') {
            $layout = 'stereo_sap';
        } elseif ($layout !== 'stereo' && $layout !== 'stereo_sap' && $layout !== '51' && $layout !== '51_sap') {
            $ac = isset($keys['AUDIO_CHANNELS']) ? trim((string)$keys['AUDIO_CHANNELS']) : '';
            $acN = ($ac !== '' && ctype_digit($ac)) ? (int)$ac : 8;
            if ($acN === 4) {
                $layout = 'stereo_sap';
            } elseif ($acN === 6 || $acN === 3 || $acN === 5) {
                $layout = '51';
            } elseif ($acN === 2) {
                $layout = 'stereo';
            } else {
                $layout = '51_sap';
            }
        }
        $embeds = parse_audio_embeds($keys['AUDIO_EMBEDS'] ?? '');
        // Transport is always 8ch Opus (HI and LO share the same track).
        $audioChannels[$path] = 8;
        $audioChannels[(string)$id] = 8;
        $audioLayouts[$path] = $layout;
        $audioLayouts[(string)$id] = $layout;
        $audioEmbeds[$path] = $embeds;
        $audioEmbeds[(string)$id] = $embeds;
    }
    echo json_encode([
        'ok' => true,
        'aliases' => $aliases,
        'devices' => $devices,
        'audio_channels' => $audioChannels,
        'audio_layouts' => $audioLayouts,
        'audio_embeds' => $audioEmbeds,
        'max_channels' => ops_max_channels(),
        'max_channel_id' => ops_max_channel_id(),
    ]);
    exit;
}

// ---- channels_list ------------------------------------------------------------

if ($action === 'channels_list') {
    $channels = [];
    foreach (list_channel_ids() as $id) {
        $r = sudo_run(['/usr/local/bin/nexvue-ops-env-read.sh', (string)$id]);
        if ($r['code'] !== 0) {
            $channels[] = ['id' => $id, 'error' => trim($r['stderr']) ?: 'read failed'];
            continue;
        }
        $data = json_decode($r['stdout'], true);
        if (!is_array($data) || empty($data['ok'])) {
            $channels[] = ['id' => $id, 'error' => 'bad helper output'];
            continue;
        }
        $keys = $data['keys'] ?? [];
        $unit = "nexvue-encode@{$id}";
        $st = parse_unit_status(sudo_run(['/usr/local/bin/nexvue-ops-status.sh', $unit])['stdout']);
        $state = $st['state'];
        $channels[] = [
            'id' => $id,
            'CHANNEL_PATH' => $keys['CHANNEL_PATH'] ?? "ch{$id}",
            'CHANNEL_ALIAS' => $keys['CHANNEL_ALIAS'] ?? '',
            'INPUT_TYPE' => $keys['INPUT_TYPE'] ?? 'decklink',
            'DEVICE_NUMBER' => $keys['DEVICE_NUMBER'] ?? (string)$id,
            'DEINT_FIELDS' => $keys['DEINT_FIELDS'] ?? '',
            'HI_PRESET' => $keys['HI_PRESET'] ?? '',
            'BITRATE_KBPS' => $keys['BITRATE_KBPS'] ?? '',
            'ENABLE_AUDIO' => $keys['ENABLE_AUDIO'] ?? '',
            'LO_ENABLE' => $keys['LO_ENABLE'] ?? '',
            'LO_PRESET' => $keys['LO_PRESET'] ?? '',
            'unit' => $unit,
            'state' => $state,
            'enabled' => $st['enabled'],
            'active' => ($state === 'active'),
        ];
    }
    echo json_encode([
        'ok' => true,
        'channels' => $channels,
        'editable_keys' => EDITABLE_KEYS,
        'max_channel_id' => ops_max_channel_id(),
        'max_channels' => ops_max_channels(),
    ]);
    exit;
}

// ---- channel_get --------------------------------------------------------------

if ($action === 'channel_get') {
    $id = $body['id'] ?? ($_GET['id'] ?? null);
    if (!channel_id_ok($id)) {
        fail(400, 'id must be 0-' . ops_max_channel_id());
    }
    $id = (int)$id;
    $r = sudo_run(['/usr/local/bin/nexvue-ops-env-read.sh', (string)$id]);
    if ($r['code'] !== 0) {
        fail(404, trim($r['stderr']) ?: 'channel not found');
    }
    $data = json_decode($r['stdout'], true);
    if (!is_array($data) || empty($data['ok'])) {
        fail(500, 'bad helper output');
    }
    $keys = $data['keys'] ?? [];
    echo json_encode([
        'ok' => true,
        'id' => $id,
        'keys' => $keys,
        'editable_keys' => EDITABLE_KEYS,
        'readonly_keys' => ['DEVICE_NUMBER', 'CHANNEL_PATH', 'RTSP_URL'],
    ]);
    exit;
}

// ---- channel_put --------------------------------------------------------------

if ($action === 'channel_put') {
    $id = $body['id'] ?? null;
    $patch = $body['patch'] ?? null;
    if (!channel_id_ok($id)) {
        fail(400, 'id must be 0-' . ops_max_channel_id());
    }
    if (!is_array($patch)) {
        fail(400, 'patch object required');
    }
    $id = (int)$id;
    $clean = [];
    foreach ($patch as $k => $v) {
        if (!is_string($k) || !in_array($k, EDITABLE_KEYS, true)) {
            fail(400, "key not editable: {$k}");
        }
        if (!is_scalar($v)) {
            fail(400, "bad value for {$k}");
        }
        $clean[$k] = (string)$v;
    }
    $r = sudo_run(
        ['/usr/local/bin/nexvue-ops-env-write.sh', (string)$id],
        json_encode($clean, JSON_UNESCAPED_SLASHES)
    );
    if ($r['code'] !== 0) {
        $err = trim($r['stderr']);
        $parsed = json_decode($err, true);
        fail(400, is_array($parsed) ? ($parsed['error'] ?? $err) : ($err ?: 'write failed'));
    }
    $data = json_decode($r['stdout'], true);
    echo json_encode([
        'ok' => true,
        'id' => $id,
        'keys' => is_array($data) ? ($data['keys'] ?? []) : [],
        'restart_units' => ["nexvue-encode@{$id}"],
    ]);
    exit;
}

// ---- channels_bulk ------------------------------------------------------------

if ($action === 'channels_bulk') {
    $ids = $body['ids'] ?? null;
    $patch = $body['patch'] ?? null;
    if (!is_array($ids) || $ids === []) {
        fail(400, 'ids array required');
    }
    if (!is_array($patch) || $patch === []) {
        fail(400, 'patch object required');
    }
    $clean = [];
    foreach ($patch as $k => $v) {
        if (!is_string($k) || !in_array($k, EDITABLE_KEYS, true)) {
            fail(400, "key not editable: {$k}");
        }
        if (!is_scalar($v)) {
            fail(400, "bad value for {$k}");
        }
        $clean[$k] = (string)$v;
    }
    $updated = [];
    $restart = [];
    $bulkIds = [];
    foreach ($ids as $rawId) {
        if (!channel_id_ok($rawId)) {
            fail(400, 'each id must be 0-' . ops_max_channel_id());
        }
        $bulkIds[] = (int)$rawId;
    }
    foreach ($bulkIds as $id) {
        $r = sudo_run(
            ['/usr/local/bin/nexvue-ops-env-write.sh', (string)$id],
            json_encode($clean, JSON_UNESCAPED_SLASHES)
        );
        if ($r['code'] !== 0) {
            $err = trim($r['stderr']);
            $parsed = json_decode($err, true);
            fail(400, "channel {$id}: " . (is_array($parsed) ? ($parsed['error'] ?? $err) : ($err ?: 'write failed')));
        }
        $updated[] = $id;
        $restart[] = "nexvue-encode@{$id}";
    }
    echo json_encode(['ok' => true, 'updated' => $updated, 'restart_units' => $restart]);
    exit;
}

// ---- restart ------------------------------------------------------------------

if ($action === 'restart') {
    $units = $body['units'] ?? null;
    if (!is_array($units) || $units === []) {
        fail(400, 'units array required');
    }
    $clean = [];
    foreach ($units as $u) {
        if (!is_string($u) || !unit_allowed($u)) {
            fail(400, "disallowed unit: {$u}");
        }
        $clean[] = $u;
    }
    $argv = array_merge(['/usr/local/bin/nexvue-ops-restart.sh'], $clean);
    $r = sudo_run($argv);
    if ($r['code'] !== 0) {
        fail(500, trim($r['stderr']) ?: 'restart failed');
    }
    echo json_encode(['ok' => true, 'restarted' => $clean]);
    exit;
}

// ---- restart_encoders (all systemd-enabled encode slots) ----------------------

if ($action === 'restart_encoders') {
    $units = [];
    foreach (list_channel_ids() as $id) {
        $unit = "nexvue-encode@{$id}";
        $st = parse_unit_status(sudo_run(['/usr/local/bin/nexvue-ops-status.sh', $unit])['stdout']);
        // Only enabled slots — disabled/parked encoders must stay parked.
        if (($st['enabled'] ?? '') === 'enabled') {
            $units[] = $unit;
        }
    }
    if ($units === []) {
        echo json_encode(['ok' => true, 'restarted' => [], 'note' => 'no enabled encoders']);
        exit;
    }
    $argv = array_merge(['/usr/local/bin/nexvue-ops-restart.sh'], $units);
    $r = sudo_run($argv);
    if ($r['code'] !== 0) {
        fail(500, trim($r['stderr']) ?: 'restart_encoders failed');
    }
    echo json_encode(['ok' => true, 'restarted' => $units]);
    exit;
}

// ---- set_enabled (encoder units only) ------------------------------------------

if ($action === 'set_enabled') {
    $unit = $body['unit'] ?? ($_GET['unit'] ?? '');
    $enable = $body['enable'] ?? null;
    if (!is_string($unit) || !unit_enable_allowed($unit)) {
        fail(400, 'unit must be nexvue-encode@0-' . MAX_CHANNEL_ID);
    }
    if (!is_bool($enable)) {
        fail(400, 'enable must be true or false');
    }
    $verb = $enable ? 'enable' : 'disable';
    $r = sudo_run(['/usr/local/bin/nexvue-ops-enable.sh', $verb, $unit]);
    if ($r['code'] !== 0) {
        fail(500, trim($r['stderr']) ?: "{$verb} failed");
    }
    echo json_encode(['ok' => true, 'unit' => $unit, 'enabled' => $enable]);
    exit;
}

// ---- set_running (encoder units only, runtime start/stop) -----------------------

if ($action === 'set_running') {
    $unit = $body['unit'] ?? ($_GET['unit'] ?? '');
    $run = $body['run'] ?? null;
    if (!is_string($unit) || !unit_enable_allowed($unit)) {
        fail(400, 'unit must be nexvue-encode@0-' . MAX_CHANNEL_ID);
    }
    if (!is_bool($run)) {
        fail(400, 'run must be true or false');
    }
    $verb = $run ? 'start' : 'stop';
    $r = sudo_run(['/usr/local/bin/nexvue-ops-enable.sh', $verb, $unit]);
    if ($r['code'] !== 0) {
        fail(500, trim($r['stderr']) ?: "{$verb} failed");
    }
    echo json_encode(['ok' => true, 'unit' => $unit, 'running' => $run]);
    exit;
}

// ---- kick_viewer (MediaMTX WebRTC session) ------------------------------------

if ($action === 'kick_viewer') {
    $sessionId = $body['session_id'] ?? ($_GET['session_id'] ?? '');
    if (!is_string($sessionId) || !kick_is_uuid($sessionId)) {
        fail(400, 'session_id must be a UUID');
    }
    $reason = kick_normalize_reason($body['reason'] ?? ($_GET['reason'] ?? ''));

    // Capture remote IP, then record the kick BEFORE tearing down MediaMTX so
    // the viewer's kick_check (fired on connection drop) cannot race an empty registry.
    $remoteIp = mediamtx_session_remote_ip($sessionId);
    try {
        kick_registry_add($sessionId, $remoteIp, $reason);
    } catch (RuntimeException $e) {
        // Continue — MediaMTX kick still useful; viewer may self-heal without message.
    }

    $r = mediamtx_http('POST', '/v3/webrtcsessions/kick/' . rawurlencode($sessionId));
    $status = $r['status'];
    if ($status === 200) {
        echo json_encode(['ok' => true, 'session_id' => $sessionId]);
        exit;
    }
    // Roll back the registry entry so a failed kick does not suppress healing.
    try {
        kick_registry_remove($sessionId);
    } catch (RuntimeException $e) {
        // ignore
    }
    if ($status === 404) {
        fail(404, 'session not found');
    }
    if ($status === 0) {
        fail(502, 'MediaMTX API unreachable');
    }
    $snippet = trim($r['body']);
    if (strlen($snippet) > 200) {
        $snippet = substr($snippet, 0, 200);
    }
    fail(502, $snippet !== '' ? "MediaMTX kick failed ({$status}): {$snippet}" : "MediaMTX kick failed ({$status})");
}

// ---- kick_check (player self-healing gate) ------------------------------------

if ($action === 'kick_check') {
    $sessionId = $body['session_id'] ?? ($_GET['session_id'] ?? '');
    if (!is_string($sessionId)) {
        $sessionId = '';
    }
    if ($sessionId !== '' && !kick_is_uuid($sessionId)) {
        fail(400, 'session_id must be a UUID');
    }
    // Session UUID only — no IP matching (shared NAT / same-channel peers).
    try {
        $result = kick_registry_check($sessionId !== '' ? $sessionId : null);
    } catch (RuntimeException $e) {
        // Fail open — do not block self-healing if the registry is unreadable.
        echo json_encode(['ok' => true, 'kicked' => false]);
        exit;
    }
    echo json_encode([
        'ok' => true,
        'kicked' => !empty($result['kicked']),
        'reason' => (string)($result['reason'] ?? ''),
    ]);
    exit;
}

// ---- logo_get / logo_put / logo_delete (station branding) ---------------------

if ($action === 'logo_get') {
    $info = logo_get_info();
    echo json_encode(array_merge(['ok' => true], $info));
    exit;
}

if ($action === 'logo_put') {
    $data = $body['data'] ?? null;
    if (!is_string($data)) {
        fail(400, 'data must be a base64 string');
    }
    try {
        $meta = logo_put_base64($data);
    } catch (InvalidArgumentException $e) {
        fail(400, $e->getMessage());
    } catch (RuntimeException $e) {
        fail(500, $e->getMessage());
    }
    echo json_encode(array_merge(['ok' => true, 'exists' => true], $meta));
    exit;
}

if ($action === 'logo_delete') {
    try {
        logo_delete();
    } catch (RuntimeException $e) {
        fail(500, $e->getMessage());
    }
    echo json_encode(['ok' => true, 'exists' => false]);
    exit;
}

// ---- network_get / network_put (admin-only public hostname / NAT IP) ----------

if ($action === 'network_get') {
    $cur = network_read_settings();
    echo json_encode(['ok' => true, 'hostname' => $cur['hostname'], 'ip' => $cur['ip']]);
    exit;
}

if ($action === 'network_put') {
    try {
        $hostname = network_sanitize_hostname((string)($body['hostname'] ?? ''));
        $ip = network_sanitize_ip((string)($body['ip'] ?? ''));
    } catch (InvalidArgumentException $e) {
        fail(400, $e->getMessage());
    }
    $helper = '/usr/local/bin/nexvue-ops-network-write.sh';
    if (!is_file($helper)) {
        fail(500, 'network helper not installed — re-run sudo ./setup.sh');
    }
    $payload = json_encode(['hostname' => $hostname, 'ip' => $ip], JSON_UNESCAPED_SLASHES);
    if (!is_string($payload)) {
        fail(500, 'failed to encode network settings');
    }
    $wr = sudo_run([$helper], $payload);
    if ($wr['code'] !== 0) {
        $err = trim($wr['stderr']);
        $decoded = json_decode($err, true);
        if (is_array($decoded) && isset($decoded['error']) && is_string($decoded['error'])) {
            $err = $decoded['error'];
        }
        if ($err === '') {
            $err = 'failed to save public reachability';
        }
        $status = (str_contains($err, 'Enter ') || str_contains($err, 'Put IP')) ? 400 : 500;
        fail($status, $err);
    }
    $restarted = false;
    $rr = sudo_run(['/usr/local/bin/nexvue-ops-restart.sh', 'mediamtx']);
    if ($rr['code'] === 0) {
        $restarted = true;
    }
    echo json_encode([
        'ok' => true,
        'hostname' => $hostname,
        'ip' => $ip,
        'restarted' => $restarted,
    ]);
    exit;
}

if ($action === 'network_test') {
    try {
        $hostname = network_sanitize_hostname((string)($body['hostname'] ?? ''));
        $ip = network_sanitize_ip((string)($body['ip'] ?? ''));
    } catch (InvalidArgumentException $e) {
        fail(400, $e->getMessage());
    }
    if ($hostname === '' && $ip === '') {
        echo json_encode([
            'ok' => true,
            'hostname' => '',
            'ip' => '',
            'status' => 'ok',
            'summary' => 'Nothing to test — leave both blank for LAN-only.',
            'checks' => [],
        ]);
        exit;
    }
    echo json_encode(array_merge(['ok' => true], network_probe($hostname, $ip)));
    exit;
}

// ---- hardware_get / hardware_put (admin-only card / encode slots) -------------

if ($action === 'hardware_get') {
    echo json_encode(['ok' => true] + hardware_read_settings());
    exit;
}

if ($action === 'hardware_put') {
    $raw = $body['slots'] ?? ($body['max_channels'] ?? null);
    if (!is_numeric($raw)) {
        fail(400, 'Encode slots must be 1–8 (Duo=2, Duo 2=4, Quad 2=8)');
    }
    $slots = (int)$raw;
    if ($slots < 1 || $slots > 8) {
        fail(400, 'Encode slots must be 1–8 (Duo=2, Duo 2=4, Quad 2=8)');
    }
    $helper = '/usr/local/bin/nexvue-ops-hardware-write.sh';
    if (!is_file($helper)) {
        fail(500, 'hardware helper not installed — re-run sudo ./setup.sh');
    }
    $payload = json_encode(['slots' => $slots], JSON_UNESCAPED_SLASHES);
    if (!is_string($payload)) {
        fail(500, 'failed to encode hardware settings');
    }
    $wr = sudo_run([$helper], $payload);
    $decoded = json_decode($wr['stdout'], true);
    if ($wr['code'] !== 0) {
        $err = trim($wr['stderr']);
        $parsed = json_decode($err, true);
        if (is_array($parsed) && isset($parsed['error']) && is_string($parsed['error'])) {
            $err = $parsed['error'];
        }
        if ($err === '' && is_array($decoded) && isset($decoded['error'])) {
            $err = (string)$decoded['error'];
        }
        if ($err === '') {
            $err = 'failed to save card / encode slots';
        }
        $status = str_contains($err, 'must be') ? 400 : 500;
        fail($status, $err);
    }
    if (!is_array($decoded) || empty($decoded['ok'])) {
        fail(500, 'bad helper output');
    }
    $out = hardware_read_settings();
    $out['ok'] = true;
    $out['seeded'] = $decoded['seeded'] ?? [];
    $out['stripped'] = $decoded['stripped'] ?? 0;
    $out['enabled'] = $decoded['enabled'] ?? [];
    $out['disabled'] = $decoded['disabled'] ?? [];
    $out['units_skipped'] = !empty($decoded['units_skipped']);
    $out['unit_errors'] = $decoded['unit_errors'] ?? [];
    echo json_encode($out);
    exit;
}

// ---- tls_status / tls_issue / tls_upload (Certificates) -----------------------

function tls_helper_or_fail(): string {
    $helper = '/usr/local/bin/nexvue-ops-tls.sh';
    if (!is_file($helper)) {
        fail(500, 'TLS helper not installed — re-run sudo ./setup.sh');
    }
    return $helper;
}

function tls_decode_helper(array $r, string $fallback): array {
    $decoded = json_decode((string)$r['stdout'], true);
    if (is_array($decoded)) {
        return $decoded;
    }
    $err = trim((string)$r['stderr']);
    $errJson = json_decode($err, true);
    if (is_array($errJson) && isset($errJson['error']) && is_string($errJson['error'])) {
        return $errJson;
    }
    return ['ok' => false, 'error' => $err !== '' ? $err : $fallback];
}

if ($action === 'tls_status') {
    $r = sudo_run([tls_helper_or_fail(), 'status']);
    $decoded = tls_decode_helper($r, 'failed to read certificate status');
    if ($r['code'] !== 0 || empty($decoded['ok'])) {
        fail(500, (string)($decoded['error'] ?? 'failed to read certificate status'));
    }
    echo json_encode($decoded);
    exit;
}

if ($action === 'tls_issue') {
    $accept = $body['accept_tos'] ?? false;
    if ($accept !== true) {
        fail(400, "Agree to the Let's Encrypt Subscriber Agreement to issue");
    }
    $payload = json_encode([
        'email' => (string)($body['email'] ?? ''),
        'domain' => (string)($body['domain'] ?? ''),
        'accept_tos' => true,
    ], JSON_UNESCAPED_SLASHES);
    if (!is_string($payload)) {
        fail(500, 'failed to encode certificate request');
    }
    $r = sudo_run([tls_helper_or_fail(), 'issue'], $payload);
    $decoded = tls_decode_helper($r, 'certificate request failed');
    if ($r['code'] !== 0 || empty($decoded['ok'])) {
        $err = (string)($decoded['error'] ?? 'certificate request failed');
        $status = (str_contains($err, 'already running')) ? 409 : 400;
        if (str_contains($err, 'not installed') || str_contains($err, 'could not start')) {
            $status = 500;
        }
        fail($status, $err);
    }
    echo json_encode($decoded);
    exit;
}

if ($action === 'tls_upload') {
    $cert = $body['cert'] ?? '';
    $key = $body['key'] ?? '';
    if (!is_string($cert) || !is_string($key)) {
        fail(400, 'cert and key must be PEM or base64 strings');
    }
    if (strlen($cert) > TLS_PEM_MAX_BYTES * 2 || strlen($key) > TLS_PEM_MAX_BYTES * 2) {
        fail(400, 'certificate or key exceeds 128 KB');
    }
    $payload = json_encode(['cert' => $cert, 'key' => $key, 'source' => 'upload'], JSON_UNESCAPED_SLASHES);
    if (!is_string($payload)) {
        fail(500, 'failed to encode certificate upload');
    }
    $r = sudo_run([tls_helper_or_fail(), 'upload'], $payload);
    $decoded = tls_decode_helper($r, 'certificate upload failed');
    if ($r['code'] !== 0 || empty($decoded['ok'])) {
        $err = (string)($decoded['error'] ?? 'certificate upload failed');
        $status = (str_contains($err, 'already running')) ? 409 : 400;
        fail($status, $err);
    }
    echo json_encode($decoded);
    exit;
}

// ---- turn_get / turn_put / turn_test (admin-only Cloudflare TURN) -------------

if ($action === 'turn_get') {
    echo json_encode(array_merge(['ok' => true], auth_turn_public()));
    exit;
}

if ($action === 'turn_put') {
    try {
        $pub = auth_turn_put([
            'enabled' => !empty($body['enabled']),
            'key_id' => (string)($body['key_id'] ?? ''),
            'api_token' => (string)($body['api_token'] ?? ''),
        ]);
    } catch (InvalidArgumentException $e) {
        fail(400, $e->getMessage());
    } catch (Throwable $e) {
        fail(500, 'failed to save Cloudflare TURN settings');
    }
    echo json_encode(array_merge(['ok' => true], $pub));
    exit;
}

if ($action === 'turn_test') {
    $keyId = trim((string)($body['key_id'] ?? ''));
    $token = trim((string)($body['api_token'] ?? ''));
    try {
        $minted = auth_turn_mint(true, $keyId !== '' ? $keyId : null, $token !== '' ? $token : null);
    } catch (InvalidArgumentException $e) {
        fail(400, $e->getMessage());
    } catch (RuntimeException $e) {
        fail(400, $e->getMessage());
    } catch (Throwable $e) {
        fail(500, 'Cloudflare TURN test failed');
    }
    echo json_encode([
        'ok' => true,
        'ice_server_count' => count($minted['ice_servers']),
        'expires_at' => $minted['expires_at'],
    ]);
    exit;
}

// ---- sfu_get / sfu_put / sfu_test (admin-only Cloudflare Stream hybrid) -------

if ($action === 'sfu_get') {
    echo json_encode(array_merge(['ok' => true], auth_sfu_public()));
    exit;
}

if ($action === 'sfu_put') {
    try {
        $pub = auth_sfu_put([
            'mode' => (string)($body['mode'] ?? 'off'),
            'account_id' => (string)($body['account_id'] ?? ''),
            'api_token' => (string)($body['api_token'] ?? ''),
        ]);
    } catch (InvalidArgumentException $e) {
        fail(400, $e->getMessage());
    } catch (RuntimeException $e) {
        fail(400, $e->getMessage());
    } catch (Throwable $e) {
        fail(500, 'failed to save Cloudflare Stream settings');
    }
    echo json_encode(array_merge(['ok' => true], $pub));
    exit;
}

if ($action === 'sfu_test') {
    $accountId = trim((string)($body['account_id'] ?? ''));
    $token = trim((string)($body['api_token'] ?? ''));
    try {
        auth_sfu_test($accountId !== '' ? $accountId : null, $token !== '' ? $token : null);
    } catch (InvalidArgumentException $e) {
        fail(400, $e->getMessage());
    } catch (RuntimeException $e) {
        fail(400, $e->getMessage());
    } catch (Throwable $e) {
        fail(500, 'Cloudflare Stream test failed');
    }
    echo json_encode(['ok' => true]);
    exit;
}

fail(400, 'unknown action');
