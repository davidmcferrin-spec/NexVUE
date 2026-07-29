<?php
/**
 * nexvue-jwks.php — public JWKS for MediaMTX JWT auth (no session required).
 *
 * MediaMTX polls this once (cached) via authJWTJWKS. Prefers the on-disk
 * jwks.json (fast); falls back to auth_ensure_keys() to generate it.
 */

declare(strict_types=1);

require_once __DIR__ . '/nexvue-auth-lib.php';

header('Content-Type: application/json');
header('Cache-Control: public, max-age=60');

try {
    $jwksPath = auth_dir() . '/jwks.json';
    if (is_readable($jwksPath)) {
        $raw = (string)file_get_contents($jwksPath);
        $jwks = json_decode($raw, true);
        if (is_array($jwks) && isset($jwks['keys'][0])) {
            echo $raw;
            exit;
        }
    }
    $keys = auth_ensure_keys();
    echo json_encode($keys['jwks'], JSON_UNESCAPED_SLASHES);
} catch (Throwable $e) {
    http_response_code(500);
    echo json_encode(['keys' => [], 'error' => 'jwks unavailable']);
}
