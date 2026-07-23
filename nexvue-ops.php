<?php
/**
 * nexvue-ops.php — JSON API for NexVUE Services + Channels ops UI.
 *
 * Phase 1 LAN-trust: no auth. Do not expose on a DMZ without Phase 2 auth.
 * Privileged work goes through allowlisted sudo wrappers only
 * (see nexvue-ops.sudoers / /usr/local/bin/nexvue-ops-*.sh).
 *
 * Actions (GET or POST JSON body):
 *   services | journal | channels_list | channel_get | channel_put
 *   | channels_bulk | restart | restart_encoders | set_enabled | set_running | aliases
 *   | kick_viewer | kick_check | logo_get | logo_put | logo_delete
 *
 * restart_encoders restarts every systemd-enabled nexvue-encode@N (parked /
 * disabled slots are left alone). set_enabled toggles systemd enable/disable
 * (with --now); set_running is
 * runtime start/stop (boot config untouched). Both apply to encoder units
 * ONLY (nexvue-encode@0-9) via nexvue-ops-enable.sh — the LAN-trust ops page
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

const CHANNELS_DIR = '/etc/nexvue/channels';
const STATION_ENV = '/etc/nexvue/nexvue.env';
/** Encoder slots 0..MAX_CHANNEL_ID (MAX_CHANNELS=10). Independent of DeckLink MAX_DEVICES. */
const MAX_CHANNEL_ID = 9;
const DEFAULT_MAX_LO_RENDITIONS = 6;
const SUDO = '/usr/bin/sudo';
/** Kick registry TTL — long enough for the 5s player reconnect window + retries. */
const KICK_REGISTRY_TTL_S = 600;
const KICK_REASON_MAX_LEN = 200;
/** Station branding logo — raw bytes + JSON metadata under /var/lib/nexvue/branding. */
const LOGO_MAX_BYTES = 1048576;
const LOGO_ALLOWED_MIMES = [
    'image/png' => true,
    'image/jpeg' => true,
    'image/webp' => true,
];

const EDITABLE_KEYS = [
    'CHANNEL_ALIAS', 'INPUT_TYPE', 'SRT_URI', 'SRT_LATENCY_MS',
    'DEINT_FIELDS', 'BITRATE_KBPS', 'GOP_FRAMES',
    'ENABLE_AUDIO', 'AUDIO_FRAME_MS', 'AUDIO_BITRATE_BPS', 'AUDIO_CHANNELS', 'AUDIO_LAYOUT',
    'DECKLINK_BUFFER_FRAMES', 'DECKLINK_DROP_NO_SIGNAL_FRAMES', 'VIDEO_ENCODER', 'EXTRA_ENC_ARGS',
    'LO_ENABLE', 'LO_PRESET', 'LO_WIDTH', 'LO_HEIGHT', 'LO_BITRATE_KBPS', 'LO_FPS',
    'LO_TARGET_USAGE', 'LO_QUEUE_BUFFERS', 'LO_GOP_FRAMES',
    'SIGNAL_LOSS_DEBOUNCE_S', 'SIGNAL_ACQUIRE_DEBOUNCE_S', 'DECKLINK_RETRY_S',
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
    return (bool)preg_match('/^nexvue-encode@[0-9]$/', $unit);
}

function channel_id_ok($id): bool {
    return is_numeric($id) && (int)$id >= 0 && (int)$id <= MAX_CHANNEL_ID;
}

/** Read KEY=value from station or channel env text (last active wins). */
function env_text_get(string $text, string $key): string {
    $last = '';
    foreach (preg_split("/\r\n|\n|\r/", $text) as $line) {
        $line = trim($line);
        if ($line === '' || str_starts_with($line, '#')) {
            continue;
        }
        if (!str_contains($line, '=')) {
            continue;
        }
        [$k, $v] = explode('=', $line, 2);
        if (trim($k) !== $key) {
            continue;
        }
        $v = trim($v);
        if (strlen($v) >= 2 && (($v[0] === '"' && str_ends_with($v, '"')) || ($v[0] === "'" && str_ends_with($v, "'")))) {
            $v = substr($v, 1, -1);
        } elseif (str_contains($v, ' #')) {
            $v = trim(explode(' #', $v, 2)[0]);
        }
        $last = $v;
    }
    return $last;
}

function station_env_int(string $key, int $default): int {
    if (!is_readable(STATION_ENV)) {
        return $default;
    }
    $raw = @file_get_contents(STATION_ENV);
    if (!is_string($raw)) {
        return $default;
    }
    $v = env_text_get($raw, $key);
    if ($v === '' || !is_numeric($v)) {
        return $default;
    }
    return (int)$v;
}

function max_lo_renditions(): int {
    $n = station_env_int('MAX_LO_RENDITIONS', DEFAULT_MAX_LO_RENDITIONS);
    return max(0, $n);
}

/**
 * Channel ids with LO_ENABLE=true, ascending (same deterministic order as
 * nexvue-supervisor.resolve_lo_enable).
 *
 * @return list<int>
 */
function lo_requester_ids(): array {
    $ids = [];
    for ($i = 0; $i <= MAX_CHANNEL_ID; $i++) {
        $path = CHANNELS_DIR . "/{$i}.env";
        if (!is_readable($path)) {
            continue;
        }
        $raw = @file_get_contents($path);
        if (!is_string($raw)) {
            continue;
        }
        if (strtolower(env_text_get($raw, 'LO_ENABLE')) === 'true') {
            $ids[] = $i;
        }
    }
    return $ids;
}

/**
 * @return array{max:int,used:int,holders:list<int>,requesters:list<int>}
 */
function lo_pool_status(): array {
    $max = max_lo_renditions();
    $requesters = lo_requester_ids();
    $holders = array_slice($requesters, 0, $max);
    return [
        'max' => $max,
        'used' => count($holders),
        'holders' => $holders,
        'requesters' => $requesters,
    ];
}

/**
 * Reject enabling LO when the floating pool is already full (UI cap).
 * Turning LO off, or re-saving true on a channel that already holds a seat, is fine.
 */
function assert_lo_enable_allowed(int $id, array $patch, ?array $bulkIds = null): void {
    if (!array_key_exists('LO_ENABLE', $patch)) {
        return;
    }
    if (strtolower(trim((string)$patch['LO_ENABLE'])) !== 'true') {
        return;
    }
    $pool = lo_pool_status();
    $max = $pool['max'];
    $requesters = $pool['requesters'];

    if ($bulkIds !== null) {
        // Simulate bulk: all listed ids become requesters; others unchanged.
        $sim = [];
        foreach ($requesters as $r) {
            if (!in_array($r, $bulkIds, true)) {
                $sim[] = $r;
            }
        }
        foreach ($bulkIds as $bid) {
            $sim[] = (int)$bid;
        }
        $sim = array_values(array_unique($sim));
        sort($sim);
        if (count($sim) > $max) {
            fail(
                400,
                "LO floating pool full: enabling LO on channels "
                . implode(',', $bulkIds)
                . " would make " . count($sim)
                . " requesters (MAX_LO_RENDITIONS={$max}). Turn LO off on other channels first."
            );
        }
        return;
    }

    if (in_array($id, $requesters, true)) {
        return; // already holds / requests a seat
    }
    if (count($requesters) >= $max) {
        $holders = implode(',', $pool['holders']);
        fail(
            400,
            "LO floating pool full ({$pool['used']}/{$max}). "
            . "Holders: [{$holders}]. Turn LO off on one channel before enabling here."
        );
    }
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

// Library mode for unit tests (php -r 'include …' without NEXVUE_OPS_HTTP).
if (PHP_SAPI === 'cli' && getenv('NEXVUE_OPS_HTTP') === false) {
    return;
}

header('Content-Type: application/json');
header('Cache-Control: no-store');

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
    return (bool)preg_match('/^(mediamtx|nexvue-status|nexvue-metrics|nexvue-encode@[0-9])$/', $unit);
}

function list_channel_ids(): array {
    $ids = [];
    for ($i = 0; $i <= MAX_CHANNEL_ID; $i++) {
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

$body = read_json_body();
$action = action_from_request($body);

// ---- services -----------------------------------------------------------------

if ($action === 'services') {
    $core = ['mediamtx', 'nexvue-status', 'nexvue-metrics'];
    $units = $core;
    foreach (list_channel_ids() as $id) {
        $units[] = "nexvue-encode@{$id}";
    }
    // Always show encode@0-9 slots that have env files; if none, still show core.
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

// ---- aliases (lightweight for player/multiview) -------------------------------

if ($action === 'aliases') {
    $aliases = [];
    $devices = [];
    $audioChannels = [];
    $audioLayouts = [];
    foreach (list_channel_ids() as $id) {
        $r = sudo_run(['/usr/local/bin/nexvue-ops-env-read.sh', (string)$id]);
        if ($r['code'] !== 0) {
            continue;
        }
        $data = json_decode($r['stdout'], true);
        if (!is_array($data) || empty($data['ok'])) {
            continue;
        }
        $keys = $data['keys'] ?? [];
        $path = $keys['CHANNEL_PATH'] ?? ("ch{$id}");
        $alias = $keys['CHANNEL_ALIAS'] ?? '';
        $label = $alias !== '' ? $alias : $path;
        $aliases[$path] = $label;
        $aliases[(string)$id] = $label;
        $dev = $keys['DEVICE_NUMBER'] ?? (string)$id;
        $devices[$path] = is_numeric($dev) ? (int)$dev : $id;
        // AUDIO_LAYOUT preferred; legacy AUDIO_CHANNELS maps to a layout.
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
            $acN = ($ac !== '' && ctype_digit($ac)) ? (int)$ac : 2;
            if ($acN === 4) {
                $layout = 'stereo_sap';
            } elseif ($acN === 6 || $acN === 3 || $acN === 5) {
                $layout = '51';
            } elseif ($acN >= 8) {
                $layout = '51_sap';
            } else {
                $layout = 'stereo';
            }
        }
        $chMap = ['stereo' => 2, 'stereo_sap' => 4, '51' => 6, '51_sap' => 8];
        $acN = $chMap[$layout] ?? 2;
        $audioChannels[$path] = $acN;
        $audioChannels[(string)$id] = $acN;
        $audioLayouts[$path] = $layout;
        $audioLayouts[(string)$id] = $layout;
    }
    echo json_encode([
        'ok' => true,
        'aliases' => $aliases,
        'devices' => $devices,
        'audio_channels' => $audioChannels,
        'audio_layouts' => $audioLayouts,
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
        $loReq = strtolower((string)($keys['LO_ENABLE'] ?? '')) === 'true';
        $pool = lo_pool_status();
        $loGranted = $loReq && in_array($id, $pool['holders'], true);
        $channels[] = [
            'id' => $id,
            'CHANNEL_PATH' => $keys['CHANNEL_PATH'] ?? "ch{$id}",
            'CHANNEL_ALIAS' => $keys['CHANNEL_ALIAS'] ?? '',
            'INPUT_TYPE' => $keys['INPUT_TYPE'] ?? 'decklink',
            'DEVICE_NUMBER' => $keys['DEVICE_NUMBER'] ?? (string)$id,
            'DEINT_FIELDS' => $keys['DEINT_FIELDS'] ?? '',
            'BITRATE_KBPS' => $keys['BITRATE_KBPS'] ?? '',
            'ENABLE_AUDIO' => $keys['ENABLE_AUDIO'] ?? '',
            'LO_ENABLE' => $keys['LO_ENABLE'] ?? '',
            'LO_GRANTED' => $loGranted,
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
        'lo_pool' => lo_pool_status(),
        'max_channel_id' => MAX_CHANNEL_ID,
    ]);
    exit;
}

// ---- channel_get --------------------------------------------------------------

if ($action === 'channel_get') {
    $id = $body['id'] ?? ($_GET['id'] ?? null);
    if (!channel_id_ok($id)) {
        fail(400, 'id must be 0-' . MAX_CHANNEL_ID);
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
    $pool = lo_pool_status();
    $loReq = strtolower((string)($keys['LO_ENABLE'] ?? '')) === 'true';
    echo json_encode([
        'ok' => true,
        'id' => $id,
        'keys' => $keys,
        'editable_keys' => EDITABLE_KEYS,
        'readonly_keys' => ['DEVICE_NUMBER', 'CHANNEL_PATH', 'RTSP_URL'],
        'lo_pool' => $pool,
        'lo_granted' => $loReq && in_array($id, $pool['holders'], true),
    ]);
    exit;
}

// ---- channel_put --------------------------------------------------------------

if ($action === 'channel_put') {
    $id = $body['id'] ?? null;
    $patch = $body['patch'] ?? null;
    if (!channel_id_ok($id)) {
        fail(400, 'id must be 0-' . MAX_CHANNEL_ID);
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
    assert_lo_enable_allowed($id, $clean, null);
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
            fail(400, 'each id must be 0-' . MAX_CHANNEL_ID);
        }
        $bulkIds[] = (int)$rawId;
    }
    assert_lo_enable_allowed(0, $clean, $bulkIds);
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

fail(400, 'unknown action');
