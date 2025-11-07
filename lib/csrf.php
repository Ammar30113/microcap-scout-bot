<?php
declare(strict_types=1);

/**
 * Guarantee that a PHP session is active before reading or writing CSRF data.
 */
function ensure_session_started(): void
{
    if (session_status() === PHP_SESSION_NONE) {
        session_start();
    }
}

/**
 * Retrieve the current CSRF token, creating one if required.
 */
function csrf_token(): string
{
    ensure_session_started();

    if (!isset($_SESSION['csrf_token'])) {
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    }

    return $_SESSION['csrf_token'];
}

/**
 * Replace the existing CSRF token with a freshly generated value.
 */
function rotate_csrf_token(): string
{
    ensure_session_started();
    $_SESSION['csrf_token'] = bin2hex(random_bytes(32));

    return $_SESSION['csrf_token'];
}

/**
 * Confirm that the provided token matches the active session token.
 */
function verify_csrf_token(?string $token): bool
{
    ensure_session_started();

    if (!is_string($token) || $token === '') {
        return false;
    }

    if (!isset($_SESSION['csrf_token'])) {
        return false;
    }

    return hash_equals($_SESSION['csrf_token'], $token);
}
