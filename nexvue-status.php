<?php
/**
 * nexvue-status.php — same-origin proxy for the nexvue-status daemon (:9998).
 *
 * The player page is served by Apache (often HTTPS). Fetching
 * https://edge:9998/status cross-origin fails when the daemon is still
 * plain HTTP (mixed content / ERR_SSL_PROTOCOL_ERROR) or when the
 * self-signed cert on :9998 was never trusted in that browser tab.
 * This script keeps the browser on Apache's origin and talks to the
 * daemon on loopback — same pattern as nexvue-metrics.php.
 *
 * Override upstream with Apache SetEnv NEXVUE_STATUS_URL
 * (e.g. https://127.0.0.1:9998). Default tries HTTP then HTTPS on :9998.
 */

declare(strict_types=1);

header('Content-Type: application/json');
header('Cache-Control: no-store');

function status_candidates(): array {
    $env = getenv('NEXVUE_STATUS_URL');
    if (is_string($env) && $env !== '') {
        return [rtrim($env, '/') . '/status'];
    }
    return [
        'http://127.0.0.1:9998/status',
        'https://127.0.0.1:9998/status',
    ];
}

function fetch_url(string $url): ?string {
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

foreach (status_candidates() as $url) {
    $body = fetch_url($url);
    if ($body !== null) {
        echo $body;
        exit;
    }
}

http_response_code(502);
echo json_encode([
    'devices' => [],
    'ts' => 0,
    'stale' => true,
    'error' => 'status daemon unreachable on loopback :9998',
]);
