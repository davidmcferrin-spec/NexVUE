<?php
/**
 * nexvue-mediamtx-api.php — same-origin proxy for MediaMTX Control API paths/list.
 *
 * Phase 3 binds MediaMTX apiAddress to 127.0.0.1:9997 so kick/config cannot
 * be reached from the DMZ. Player / Multiview still need /v3/paths/list for
 * viewer counts, egress Mbps, and LO readiness — this script keeps the
 * browser on Apache's origin and fetches loopback (same pattern as
 * nexvue-status.php).
 *
 * Allowlisted GET only. Override upstream with Apache SetEnv
 * NEXVUE_MEDIAMTX_API_URL (must be loopback; default https://127.0.0.1:9997).
 */

declare(strict_types=1);

require_once __DIR__ . '/nexvue-auth-lib.php';

header('Content-Type: application/json');
header('Cache-Control: no-store');

try {
    if (!auth_bypass_enabled()) {
        auth_require_any();
        auth_session_release();
    }
} catch (RuntimeException $e) {
    auth_session_release();
    http_response_code($e->getMessage() === 'unauthorized' ? 401 : 403);
    echo json_encode(['error' => 'unauthorized']);
    exit;
} catch (Throwable $e) {
    auth_session_release();
    http_response_code(500);
    echo json_encode(['error' => 'auth store unavailable']);
    exit;
}

function mediamtx_api_proxy_base(): string {
    $base = getenv('NEXVUE_MEDIAMTX_API_URL');
    if (!is_string($base) || $base === '') {
        $base = 'https://127.0.0.1:9997';
    }
    $base = rtrim($base, '/');
    $host = parse_url($base, PHP_URL_HOST);
    if (!is_string($host) || !in_array(strtolower($host), ['127.0.0.1', 'localhost', '::1'], true)) {
        throw new RuntimeException('NEXVUE_MEDIAMTX_API_URL must be loopback');
    }
    return $base;
}

/** @return list<string> */
function mediamtx_api_proxy_candidates(string $path): array {
    $base = mediamtx_api_proxy_base();
    $primary = $base . $path;
    $out = [$primary];
    // Prefer configured URL; if TLS is off on the API, try the other scheme.
    if (str_starts_with($primary, 'https://')) {
        $out[] = 'http://' . substr($primary, strlen('https://'));
    } elseif (str_starts_with($primary, 'http://')) {
        $out[] = 'https://' . substr($primary, strlen('http://'));
    }
    return $out;
}

function mediamtx_api_proxy_fetch(string $url): ?string {
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        if ($ch === false) {
            return null;
        }
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 2,
            CURLOPT_TIMEOUT => 5,
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
            'timeout' => 5.0,
            'ignore_errors' => true,
            'header' => "Accept: application/json\r\n",
        ],
        'ssl' => [
            'verify_peer' => false,
            'verify_peer_name' => false,
        ],
    ]);
    $body = @file_get_contents($url, false, $ctx);
    if ($body === false || $body === '') {
        return null;
    }
    global $http_response_header;
    if (is_array($http_response_header) && isset($http_response_header[0])) {
        if (!preg_match('/\s2\d\d\s/', $http_response_header[0])) {
            return null;
        }
    }
    return $body;
}

// Only paths/list is needed by Player / Multiview. Kick stays on nexvue-ops.php.
$path = '/v3/paths/list';

try {
    foreach (mediamtx_api_proxy_candidates($path) as $url) {
        $body = mediamtx_api_proxy_fetch($url);
        if ($body !== null) {
            echo $body;
            exit;
        }
    }
} catch (RuntimeException $e) {
    http_response_code(500);
    echo json_encode(['error' => $e->getMessage()]);
    exit;
}

http_response_code(502);
echo json_encode([
    'error' => 'MediaMTX API unreachable on loopback :9997',
    'items' => [],
]);
