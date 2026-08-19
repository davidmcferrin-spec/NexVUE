<?php
/**
 * nexvue-logo.php — stream the station branding logo (if uploaded).
 *
 * Reads /var/lib/nexvue/branding/logo.bin (+ logo.json for Content-Type).
 * Returns 404 when no logo is present so <img> stays hidden via onerror.
 * Override storage with NEXVUE_BRANDING_DIR (same as nexvue-ops.php).
 */

declare(strict_types=1);

function branding_dir(): string {
    $override = getenv('NEXVUE_BRANDING_DIR');
    if (is_string($override) && $override !== '') {
        return rtrim($override, '/');
    }
    return '/var/lib/nexvue/branding';
}

$dir = branding_dir();
$bin = $dir . '/logo.bin';
$metaPath = $dir . '/logo.json';

if (!is_readable($bin) || !is_file($bin)) {
    http_response_code(404);
    header('Cache-Control: no-store');
    exit;
}

$bytes = filesize($bin);
if ($bytes === false || $bytes < 1) {
    http_response_code(404);
    header('Cache-Control: no-store');
    exit;
}

$mime = 'application/octet-stream';
if (is_readable($metaPath)) {
    $raw = @file_get_contents($metaPath);
    if (is_string($raw) && $raw !== '') {
        $meta = json_decode($raw, true);
        if (is_array($meta) && isset($meta['mime']) && is_string($meta['mime'])) {
            $allowed = ['image/png' => true, 'image/jpeg' => true, 'image/webp' => true];
            if (isset($allowed[strtolower($meta['mime'])])) {
                $mime = strtolower($meta['mime']);
            }
        }
    }
}

$mtime = filemtime($bin);
$etag = '"' . dechex(is_int($mtime) ? $mtime : 0) . '-' . dechex((int)$bytes) . '"';
if (isset($_SERVER['HTTP_IF_NONE_MATCH']) && trim((string)$_SERVER['HTTP_IF_NONE_MATCH']) === $etag) {
    http_response_code(304);
    header('ETag: ' . $etag);
    header('Cache-Control: private, max-age=60');
    exit;
}

header('Content-Type: ' . $mime);
header('Content-Length: ' . (string)(int)$bytes);
header('ETag: ' . $etag);
header('Cache-Control: private, max-age=60');
readfile($bin);
exit;
