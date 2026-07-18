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
 *   | channels_bulk | restart | set_enabled | aliases | kick_viewer | kick_check
 *
 * set_enabled toggles systemd enable/disable (with --now) for encoder units
 * ONLY (nexvue-encode@0-7) via nexvue-ops-enable.sh — the LAN-trust ops page
 * must not be able to disable mediamtx or the shared daemons.
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
const SUDO = '/usr/bin/sudo';
/** Kick registry TTL — long enough for the 5s player reconnect window + retries. */
const KICK_REGISTRY_TTL_S = 600;
const KICK_REASON_MAX_LEN = 200;

const EDITABLE_KEYS = [
    'CHANNEL_ALIAS', 'MAX_DEVICES', 'DEINT_FIELDS', 'BITRATE_KBPS', 'GOP_FRAMES',
    'ENABLE_AUDIO', 'AUDIO_FRAME_MS', 'AUDIO_BITRATE_BPS', 'AUDIO_CHANNELS',
    'DECKLINK_BUFFER_FRAMES', 'VIDEO_ENCODER', 'EXTRA_ENC_ARGS',
    'LO_ENABLE', 'LO_PRESET', 'LO_WIDTH', 'LO_HEIGHT', 'LO_BITRATE_KBPS', 'LO_FPS',
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
    return (bool)preg_match('/^nexvue-encode@[0-7]$/', $unit);
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
    return (bool)preg_match('/^(mediamtx|nexvue-status|nexvue-metrics|nexvue-encode@[0-7])$/', $unit);
}

function list_channel_ids(): array {
    $ids = [];
    for ($i = 0; $i <= 7; $i++) {
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

// ---- aliases (lightweight for player/multiview) -------------------------------

if ($action === 'aliases') {
    $aliases = [];
    $devices = [];
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
    }
    echo json_encode(['ok' => true, 'aliases' => $aliases, 'devices' => $devices]);
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
            'DEVICE_NUMBER' => $keys['DEVICE_NUMBER'] ?? (string)$id,
            'DEINT_FIELDS' => $keys['DEINT_FIELDS'] ?? '',
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
    echo json_encode(['ok' => true, 'channels' => $channels, 'editable_keys' => EDITABLE_KEYS]);
    exit;
}

// ---- channel_get --------------------------------------------------------------

if ($action === 'channel_get') {
    $id = $body['id'] ?? ($_GET['id'] ?? null);
    if (!is_numeric($id) || (int)$id < 0 || (int)$id > 7) {
        fail(400, 'id must be 0-7');
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
    echo json_encode([
        'ok' => true,
        'id' => $id,
        'keys' => $data['keys'] ?? [],
        'editable_keys' => EDITABLE_KEYS,
        'readonly_keys' => ['DEVICE_NUMBER', 'CHANNEL_PATH', 'RTSP_URL'],
    ]);
    exit;
}

// ---- channel_put --------------------------------------------------------------

if ($action === 'channel_put') {
    $id = $body['id'] ?? null;
    $patch = $body['patch'] ?? null;
    if (!is_numeric($id) || (int)$id < 0 || (int)$id > 7) {
        fail(400, 'id must be 0-7');
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
    foreach ($ids as $rawId) {
        if (!is_numeric($rawId) || (int)$rawId < 0 || (int)$rawId > 7) {
            fail(400, 'each id must be 0-7');
        }
        $id = (int)$rawId;
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

// ---- set_enabled (encoder units only) ------------------------------------------

if ($action === 'set_enabled') {
    $unit = $body['unit'] ?? ($_GET['unit'] ?? '');
    $enable = $body['enable'] ?? null;
    if (!is_string($unit) || !unit_enable_allowed($unit)) {
        fail(400, 'unit must be nexvue-encode@0-7');
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

fail(400, 'unknown action');
