"""Bot process の起動と複数 token 監督。

これはユーザー向け機能ではないため、意図的に ``features`` の外へ置く。
Discord login retry、token 無効時の backoff、複数 Bot の子 process 監督など、
application 起動時の関心を所有する。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

import discord


def log_and_backoff_for_token_invalid(ctx: Any, error_msg: str) -> None:
    """永続的な token 失敗をログに出し、process 終了前に待機する。

    引数:
        ctx: ``logger``、``time``、``TOKEN_INVALID_BACKOFF_SECONDS`` を提供する
            runtime module として扱う。
        error_msg: 人間が読める Discord 認証失敗メッセージ。
    """
    ctx.logger.error(
        f"{error_msg} — DISCORD_TOKEN または DISCORD_TOKENS を確認してください "
        f"（Discord Developer Portal で再生成 → 環境変数を更新 → redeploy）"
        f" / {ctx.TOKEN_INVALID_BACKOFF_SECONDS}秒待機して exit します"
    )
    ctx.time.sleep(ctx.TOKEN_INVALID_BACKOFF_SECONDS)


def run_single_bot(ctx: Any, discord_token: str) -> None:
    """一時的な Discord 障害に retry しながら token 1 件で Bot を起動する。

    引数:
        ctx: 設定済み Discord client と logger を所有する runtime module。
        discord_token: ``discord.Client.run`` へ渡す token。
    """
    for attempt in range(ctx.MAX_LOGIN_RETRIES):
        try:
            ctx.client.run(discord_token)
            break
        except discord.LoginFailure as e:
            log_and_backoff_for_token_invalid(ctx, f"Discordログイン失敗: {e}")
            raise
        except discord.ConnectionClosed as e:
            if getattr(e, "code", None) == 4004:
                log_and_backoff_for_token_invalid(
                    ctx,
                    f"Discord認証失敗 (4004): セッション中にトークン無効化 ({e})",
                )
            raise
        except discord.DiscordServerError as e:
            if attempt == ctx.MAX_LOGIN_RETRIES - 1:
                ctx.logger.error(f"最大リトライ回数到達、諦めます: {e}")
                raise
            wait = 5 * (2**attempt)
            ctx.logger.warning(
                f"Discord API一時障害 ({attempt + 1}/{ctx.MAX_LOGIN_RETRIES}): {e}"
                f" → {wait}秒待機して再試行"
            )
            ctx.time.sleep(wait)


def terminate_processes(ctx: Any, processes: list[Any]) -> None:
    """子 Bot process を SIGTERM で止め、必要なら SIGKILL 相当で fallback する。

    引数:
        ctx: process の待機挙動を提供する runtime module。
        processes: 子 ``subprocess.Popen`` 相当 object。
    """
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
    for proc in processes:
        if proc.poll() is None:
            try:
                proc.wait(timeout=10)
            except ctx.subprocess.TimeoutExpired:
                proc.kill()


def _new_failure_times() -> deque[float]:
    """子 process 監督用の型付き crash timestamp 列を作る。"""
    return deque()


@dataclass
class ChildBotSlot:
    """子 Bot process 1 件分の監督状態。"""

    instance: int
    token: str
    process: Any
    failure_times: deque[float] = field(default_factory=_new_failure_times)
    next_restart_at: float = 0.0


def spawn_child_bot(ctx: Any, token: str, instance: int, script_path: str) -> Any:
    """子 Bot process を 1 件起動する。

    引数:
        ctx: 環境変数と subprocess helper を提供する runtime module。
        token: 子 process に割り当てる Discord token。
        instance: ログに出す 1 始まりの instance 番号。
        script_path: top-level の Bot script path。

    戻り値:
        ``subprocess.Popen`` 相当の子 process。
    """
    child_env = ctx.os.environ.copy()
    child_env["DISCORD_TOKEN"] = token
    child_env["DISCORD_TOKENS"] = ""
    child_env["MULTIBOT_CHILD"] = "1"
    child_env["BOT_INSTANCE_INDEX"] = str(instance)
    child_env["RUN_DB_MIGRATIONS"] = "0"
    proc = ctx.subprocess.Popen([ctx.sys.executable, script_path], env=child_env)
    ctx.logger.info(f"Botプロセス起動: instance={instance}, pid={proc.pid}")
    return proc


def run_multi_bots(ctx: Any, discord_tokens: list[str]) -> None:
    """複数 token を子 process として起動し、落ちた子を再起動する。

    引数:
        ctx: process、signal、time、logger API を提供する runtime module。
        discord_tokens: 監督対象の正規化済み Discord token。
    """
    script_path = ctx.os.path.abspath(ctx.__file__)
    slots: list[ChildBotSlot] = []
    shutdown_requested = False
    ctx.logger.info(f"複数Botモードで起動: {len(discord_tokens)}プロセス")

    def _shutdown(_signum: int, _frame: FrameType | None) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        raise KeyboardInterrupt

    previous_sigterm = ctx.signal.signal(ctx.signal.SIGTERM, _shutdown)
    try:
        for idx, token in enumerate(discord_tokens, start=1):
            slots.append(
                ChildBotSlot(
                    instance=idx,
                    token=token,
                    process=spawn_child_bot(ctx, token, idx, script_path),
                )
            )

        while not shutdown_requested:
            now = ctx.time.monotonic()

            for slot in slots:
                if shutdown_requested:
                    break
                if slot.next_restart_at and now >= slot.next_restart_at:
                    slot.process = spawn_child_bot(
                        ctx, slot.token, slot.instance, script_path
                    )
                    slot.next_restart_at = 0.0

            for slot in slots:
                if slot.next_restart_at:
                    continue
                code = slot.process.poll()
                if code is None:
                    continue
                slot.failure_times.append(now)
                while (
                    slot.failure_times
                    and now - slot.failure_times[0] > ctx.BOT_CRASH_WINDOW_SECONDS
                ):
                    slot.failure_times.popleft()
                if len(slot.failure_times) >= ctx.BOT_CRASH_THRESHOLD:
                    raise RuntimeError(
                        f"クラッシュループ検出 instance={slot.instance}: "
                        f"{ctx.BOT_CRASH_WINDOW_SECONDS}秒に"
                        f"{len(slot.failure_times)}回終了 (last_exit={code})"
                    )
                backoff = min(
                    2 ** (len(slot.failure_times) - 1),
                    ctx.BOT_RESTART_BACKOFF_MAX_SECONDS,
                )
                slot.next_restart_at = now + backoff
                ctx.logger.warning(
                    f"Botプロセス終了 instance={slot.instance} "
                    f"pid={slot.process.pid} exit={code} "
                    f"→ {backoff}秒後に再起動 "
                    f"(直近終了 {len(slot.failure_times)}/{ctx.BOT_CRASH_THRESHOLD})"
                )

            pending = [s.next_restart_at for s in slots if s.next_restart_at]
            if pending:
                sleep_for = min(
                    ctx.BOT_POLL_INTERVAL_SECONDS,
                    max(0.0, min(pending) - ctx.time.monotonic()),
                )
            else:
                sleep_for = ctx.BOT_POLL_INTERVAL_SECONDS
            if sleep_for > 0:
                ctx.time.sleep(sleep_for)
    except KeyboardInterrupt:
        ctx.logger.info("終了シグナルを受信、全Botプロセスを停止します")
    finally:
        ctx.signal.signal(ctx.signal.SIGTERM, previous_sigterm)
        terminate_processes(ctx, [slot.process for slot in slots])


def main(ctx: Any) -> None:
    """起動設定を検証し、単一 Bot または複数 Bot mode を起動する。"""
    tokens = ctx._resolve_discord_tokens()
    if not tokens:
        raise RuntimeError(
            "DISCORD_TOKEN または DISCORD_TOKENS environment variable が必要です"
        )
    if not ctx.DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is required")
    if not ctx.ENGINES:
        raise RuntimeError("VOICEVOX_URL など、少なくとも1つのTTSエンジンURLが必要です")

    try:
        import uvloop

        uvloop.install()
        ctx.logger.info("uvloop を有効化しました")
    except ImportError:
        ctx.logger.info("uvloop 未インストール、標準 asyncio ループで起動")

    ctx.logger.info(
        f"起動モード: {'child' if ctx.IS_MULTIBOT_CHILD else 'single'}, "
        f"instance={ctx.BOT_INSTANCE_INDEX}, tokens={len(tokens)}"
    )
    if len(tokens) > 1 and not ctx.IS_MULTIBOT_CHILD:
        ctx.logger.info("複数Botモードの事前処理としてDBマイグレーションを実行します")
        ctx.asyncio.run(
            ctx.migration_runner.run_pending_migrations(
                ctx.DATABASE_URL, logger=ctx.logger
            )
        )
        run_multi_bots(ctx, tokens)
    else:
        run_single_bot(ctx, tokens[0])
