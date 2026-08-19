<?php
/**
 * nexvue-jwks.php — public JWKS for MediaMTX JWT auth (no session required).
 *
 * MediaMTX polls this once (cached) via authJWTJWKS. Serves the local key
 * (always present, fast on-disk jwks.json path — falls back to
 * auth_ensure_keys() to generate it) merged with a locally-cached copy of
 * the portal's public key once this station is adopted (Phase 4). This is
 * the only thing that changes on adopt/un-adopt — authJWTJWKS in
 * mediamtx.yml always points here, unmodified, either way. No network call
 * happens here, so a portal outage never affects JWT verification.
 */

declare(strict_types=1);

require_once __DIR__ . '/nexvue-auth-lib.php';

header('Content-Type: application/json');
header('Cache-Control: public, max-age=60');

try {
    $cachePath = auth_portal_jwks_cache_path();
    if (!is_readable($cachePath)) {
        // Fast, common path: no portal key cached — serve the local set
        // exactly as before (on-disk jwks.json, no merge work at all).
        $jwksPath = auth_dir() . '/jwks.json';
        if (is_readable($jwksPath)) {
            $raw = (string)file_get_contents($jwksPath);
            $jwks = json_decode($raw, true);
            if (is_array($jwks) && isset($jwks['keys'][0])) {
                echo $raw;
                exit;
            }
        }
        echo json_encode(auth_ensure_keys()['jwks'], JSON_UNESCAPED_SLASHES);
        exit;
    }
    echo json_encode(auth_merged_jwks(), JSON_UNESCAPED_SLASHES);
} catch (Throwable $e) {
    http_response_code(500);
    echo json_encode(['keys' => [], 'error' => 'jwks unavailable']);
}
