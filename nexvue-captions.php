<?php
/**
 * nexvue-captions.php — same-origin Server-Sent Events for caption cues.
 *
 * The encode pipeline writes /run/nexvue/captions/<channel>.json (CEA-608/CC1
 * text). This script streams those updates to Player / Multiview / Cast over
 * Apache — no new port, no MediaMTX involvement.
 *
 *   GET nexvue-captions.php?channel=ch0          → text/event-stream (SSE)
 *   GET nexvue-captions.php?channel=ch0&once=1   → application/json snapshot
 *
 * Override state directory with Apache SetEnv NEXVUE_CAPTIONS_DIR.
 *
 * Library mode (tests): leave NEXVUE_CAPTIONS_HTTP unset and include this file
 * to call captions_* helpers.
 */

declare(strict_types=1);

function captions_state_dir(): string {
    $env = getenv('NEXVUE_CAPTIONS_DIR');
    if (is_string($env) && $env !== '') {
        return rtrim($env, "/\\");
    }
    return '/run/nexvue/captions';
}

function captions_stale_after_s(): float {
    $env = getenv('NEXVUE_CAPTIONS_STALE_S');
    if (is_string($env) && $env !== '' && is_numeric($env)) {
        return (float)$env;
    }
    return 60.0;
}

function captions_normalize_channel(?string $channel): ?string {
    if ($channel === null || $channel === '') {
        return null;
    }
    $channel = trim($channel);
    if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/', $channel)) {
        return null;
    }
    return $channel;
}

/**
 * @return array{channel:string,text:string,clear:bool,ts:float,seq:int,service:string}
 */
function captions_empty_state(string $channel): array {
    return [
        'channel' => $channel,
        'text' => '',
        'clear' => true,
        'ts' => 0.0,
        'seq' => 0,
        'service' => 'CC1',
    ];
}

/**
 * @return array{channel:string,text:string,clear:bool,ts:float,seq:int,service:string}
 */
function captions_read_state(string $channel): array {
    $path = captions_state_dir() . '/' . $channel . '.json';
    if (!is_readable($path)) {
        return captions_empty_state($channel);
    }
    $raw = @file_get_contents($path);
    if ($raw === false || $raw === '') {
        return captions_empty_state($channel);
    }
    $data = json_decode($raw, true);
    if (!is_array($data)) {
        return captions_empty_state($channel);
    }
    $text = isset($data['text']) ? (string)$data['text'] : '';
    // Cap payload size for clients; strip ASCII control chars except \n/\t.
    if (strlen($text) > 2000) {
        $text = substr($text, 0, 2000);
    }
    $text = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/', '', $text) ?? '';
    // Dead-writer guard: the decoder idle-erases after ~16s of caption
    // silence, so a non-empty state file that hasn't been touched in far
    // longer means the decoder/encoder died mid-caption. Serve it cleared
    // rather than freezing the last words on every viewer's screen.
    if ($text !== '') {
        clearstatcache(true, $path);
        $mtime = @filemtime($path);
        if ($mtime !== false && (time() - $mtime) > captions_stale_after_s()) {
            $text = '';
        }
    }
    return [
        'channel' => $channel,
        'text' => $text,
        'clear' => $text === '' || !empty($data['clear']),
        'ts' => isset($data['ts']) ? (float)$data['ts'] : 0.0,
        'seq' => isset($data['seq']) ? (int)$data['seq'] : 0,
        'service' => isset($data['service']) ? (string)$data['service'] : 'CC1',
    ];
}

function captions_sse_encode(array $state): string {
    return 'data: ' . json_encode($state, JSON_UNESCAPED_UNICODE) . "\n\n";
}

// Library mode for unit tests (php -r 'include …' without NEXVUE_CAPTIONS_HTTP).
if (PHP_SAPI === 'cli' && getenv('NEXVUE_CAPTIONS_HTTP') === false) {
    return;
}

// ---- HTTP entrypoint --------------------------------------------------------
$channel = captions_normalize_channel($_GET['channel'] ?? null);
if ($channel === null) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'channel required (alphanumeric)']);
    exit;
}

$once = isset($_GET['once']) && (string)$_GET['once'] !== '' && (string)$_GET['once'] !== '0';
if ($once) {
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo json_encode(captions_read_state($channel), JSON_UNESCAPED_UNICODE);
    exit;
}

// SSE stream
header('Content-Type: text/event-stream; charset=utf-8');
header('Cache-Control: no-store');
header('Connection: keep-alive');
header('X-Accel-Buffering: no');
// mod_deflate buffers compressed responses until it has a full block —
// which turns a live event stream into delayed batches. Force it off for
// this response, along with PHP's own zlib/output buffering.
if (function_exists('apache_setenv')) {
    @apache_setenv('no-gzip', '1');
}
@ini_set('zlib.output_compression', '0');
while (ob_get_level() > 0) {
    ob_end_flush();
}
ignore_user_abort(false);
set_time_limit(0);

$lastSeq = -1;
$lastText = null;
$ticks = 0;
$maxTicks = 12000; // ~10 min at 50ms — client reconnects

// Fast reconnect after the 10-min cap or a network blip (browser default ~3s).
echo "retry: 1000\n";
echo ": nexvue-captions\n\n";
flush();

while (!connection_aborted() && $ticks < $maxTicks) {
    $state = captions_read_state($channel);
    $seq = (int)$state['seq'];
    $text = (string)$state['text'];
    if ($seq !== $lastSeq || $text !== $lastText) {
        echo captions_sse_encode($state);
        flush();
        $lastSeq = $seq;
        $lastText = $text;
    } elseif ($ticks > 0 && ($ticks % 100) === 0) {
        // Heartbeat every ~5s so proxies do not idle-close the stream.
        echo ": ping\n\n";
        flush();
    }
    $ticks++;
    usleep(50000);
}
