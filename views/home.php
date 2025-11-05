<?php
declare(strict_types=1);

require_once __DIR__ . '/../lib/csrf.php';

ensure_session_started();
$csrfToken = csrf_token();
$messages = isset($messages) && is_iterable($messages) ? $messages : [];
?>
<section class="messages">
    <?php foreach ($messages as $message): ?>
        <article class="message" data-message-id="<?= htmlspecialchars((string) ($message['id'] ?? ''), ENT_QUOTES, 'UTF-8') ?>">
            <header class="message__header">
                <h2 class="message__nickname"><?= htmlspecialchars((string) ($message['nickname'] ?? 'Anonymous'), ENT_QUOTES, 'UTF-8') ?></h2>
                <?php if (isset($message['created_at'])): ?>
                    <time class="message__timestamp" datetime="<?= htmlspecialchars((string) $message['created_at'], ENT_QUOTES, 'UTF-8') ?>">
                        <?= htmlspecialchars((string) $message['created_at'], ENT_QUOTES, 'UTF-8') ?>
                    </time>
                <?php endif; ?>
            </header>
            <p class="message__body"><?= nl2br(htmlspecialchars((string) ($message['body'] ?? ''), ENT_QUOTES, 'UTF-8')) ?></p>

            <section class="comments" data-comments>
                <?php $comments = $message['comments'] ?? []; ?>
                <?php if ($comments === []): ?>
                    <p class="comments__empty" data-empty-state>Be the first to comment.</p>
                <?php else: ?>
                    <?php foreach ($comments as $comment): ?>
                        <div class="comment" data-comment-id="<?= htmlspecialchars((string) $comment['id'], ENT_QUOTES, 'UTF-8') ?>">
                            <p class="comment__meta">
                                <span class="comment__nickname"><?= htmlspecialchars((string) $comment['nickname'], ENT_QUOTES, 'UTF-8') ?></span>
                                <time class="comment__timestamp" datetime="<?= htmlspecialchars((string) $comment['created_at'], ENT_QUOTES, 'UTF-8') ?>">
                                    <?= htmlspecialchars((string) $comment['created_at'], ENT_QUOTES, 'UTF-8') ?>
                                </time>
                            </p>
                            <p class="comment__body"><?= nl2br(htmlspecialchars((string) $comment['body'], ENT_QUOTES, 'UTF-8')) ?></p>
                        </div>
                    <?php endforeach; ?>
                <?php endif; ?>
            </section>

            <form class="comment-form" data-comment-form action="/comments.php" method="post">
                <input type="hidden" name="message_id" value="<?= htmlspecialchars((string) ($message['id'] ?? ''), ENT_QUOTES, 'UTF-8') ?>">
                <input type="hidden" name="csrf_token" value="<?= htmlspecialchars($csrfToken, ENT_QUOTES, 'UTF-8') ?>">
                <div class="comment-form__field">
                    <label>
                        <span class="comment-form__label">Nickname</span>
                        <input type="text" name="nickname" maxlength="64" required placeholder="Agent Mulder">
                    </label>
                </div>
                <div class="comment-form__field">
                    <label>
                        <span class="comment-form__label">Comment</span>
                        <textarea name="body" rows="3" maxlength="240" required placeholder="Trust no one..."></textarea>
                    </label>
                </div>
                <p class="comment-form__error" data-comment-error role="alert" hidden></p>
                <button type="submit" class="comment-form__submit">Reply</button>
            </form>
        </article>
    <?php endforeach; ?>
</section>

<script>
(() => {
    function escapeHtml(value) {
        return value
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function renderComment(comment) {
        const wrapper = document.createElement('div');
        wrapper.className = 'comment';
        wrapper.dataset.commentId = String(comment.id);
        const createdAt = comment.created_at ? new Date(comment.created_at) : null;
        const timestamp = createdAt && !Number.isNaN(createdAt.valueOf())
            ? createdAt.toLocaleString()
            : comment.created_at || '';

        wrapper.innerHTML = `
            <p class="comment__meta">
                <span class="comment__nickname">${escapeHtml(comment.nickname ?? 'Anonymous')}</span>
                <time class="comment__timestamp" datetime="${escapeHtml(comment.created_at ?? '')}">${escapeHtml(timestamp)}</time>
            </p>
            <p class="comment__body">${escapeHtml(comment.body ?? '')}</p>
        `;

        return wrapper;
    }

    async function submitComment(form) {
        const messageId = form.querySelector('input[name="message_id"]').value;
        const nicknameField = form.querySelector('input[name="nickname"]');
        const bodyField = form.querySelector('textarea[name="body"]');
        const csrfField = form.querySelector('input[name="csrf_token"]');
        const errorField = form.querySelector('[data-comment-error]');

        const payload = {
            message_id: Number.parseInt(messageId, 10),
            nickname: nicknameField.value.trim(),
            body: bodyField.value.trim(),
            csrf_token: csrfField.value,
        };

        if (payload.body.length > 240) {
            errorField.textContent = 'Comments must be 240 characters or fewer.';
            errorField.hidden = false;
            return;
        }

        try {
            const response = await fetch('/comments.php', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                body: JSON.stringify(payload),
            });

            const data = await response.json();
            if (data.csrfToken) {
                csrfField.value = data.csrfToken;
            }

            if (!response.ok) {
                throw new Error(data.error || 'Failed to post comment.');
            }

            errorField.hidden = true;
            errorField.textContent = '';

            const commentsContainer = form.parentElement.querySelector('[data-comments]');
            const emptyState = commentsContainer.querySelector('[data-empty-state]');
            if (emptyState) {
                emptyState.remove();
            }

            const commentElement = renderComment(data.comment);
            commentsContainer.appendChild(commentElement);
            bodyField.value = '';
        } catch (error) {
            errorField.textContent = error.message;
            errorField.hidden = false;
        }
    }

    document.querySelectorAll('[data-comment-form]').forEach((form) => {
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            submitComment(form);
        });
    });
})();
</script>
