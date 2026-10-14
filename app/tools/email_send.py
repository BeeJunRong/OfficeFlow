"""邮件发送工具（FR-03 的"协议"部分）。

Q-02 合规设计（guardsrails §1：只生成草稿不自动发送，必须 100% 满足）：
1. **生成流程绝不调用本模块**。发送只经由 `POST /api/email/send` 显式触发。
2. **默认协议 `draft_only`**：只返回预览，绝不产生任何网络发送行为。
3. `confirm=False` 时一律只预览，即使协议是 smtp。
4. `smtp` 协议必须先在 .env 配好；未配置时返回**明确错误**，绝不谎报成功。
5. 每个收件人独立返回 status（sent / preview / skipped / failed），便于逐封核对。

环境变量（仅 smtp 协议需要；不配置则 smtp 不可用）：
- OFFICEFLOW_SMTP_HOST / OFFICEFLOW_SMTP_PORT（默认 587）
- OFFICEFLOW_SMTP_USER / OFFICEFLOW_SMTP_PASSWORD
- OFFICEFLOW_SMTP_FROM（默认取 USER）
- OFFICEFLOW_SMTP_TLS（默认 1，使用 STARTTLS）
- OFFICEFLOW_EMAIL_ENABLE_SEND（默认 0；必须显式设为 1 才允许真实发送）
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

from app.schemas import (
    DEFAULT_EMAIL_PROTOCOL,
    EmailRecipient,
    EmailSendResponse,
    EmailSendResult,
)

logger = logging.getLogger("officeflow.email")

VALID_PROTOCOLS = ("draft_only", "mailto", "smtp")


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def smtp_config() -> dict[str, Any]:
    """读取 SMTP 配置；缺失关键项时 available=False。"""
    host = os.getenv("OFFICEFLOW_SMTP_HOST", "").strip()
    user = os.getenv("OFFICEFLOW_SMTP_USER", "").strip()
    password = os.getenv("OFFICEFLOW_SMTP_PASSWORD", "").strip()
    sender = os.getenv("OFFICEFLOW_SMTP_FROM", "").strip() or user
    try:
        port = int(os.getenv("OFFICEFLOW_SMTP_PORT", "587"))
    except ValueError:
        port = 587

    missing = [
        name
        for name, value in (
            ("OFFICEFLOW_SMTP_HOST", host),
            ("OFFICEFLOW_SMTP_USER", user),
            ("OFFICEFLOW_SMTP_PASSWORD", password),
            ("OFFICEFLOW_SMTP_FROM(或 USER)", sender),
        )
        if not value
    ]
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "sender": sender,
        "tls": _env_flag("OFFICEFLOW_SMTP_TLS", "1"),
        "enabled": _env_flag("OFFICEFLOW_EMAIL_ENABLE_SEND", "0"),
        "missing": missing,
        "available": not missing,
    }


def _build_message(
    *, sender: str, to_addr: str, subject: str, body: str
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject or "(无主题)"
    msg.set_content(body or "")
    return msg


def _send_one_smtp(cfg: dict[str, Any], to_addr: str, subject: str, body: str) -> None:
    """真实发送一封（仅在 smtp 协议 + confirm + enabled 时被调用）。"""
    msg = _build_message(
        sender=cfg["sender"], to_addr=to_addr, subject=subject, body=body
    )
    if cfg["port"] == 465:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30) as server:
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        return
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
        if cfg["tls"]:
            server.starttls()
        server.login(cfg["user"], cfg["password"])
        server.send_message(msg)


def mailto_link(to_addr: str, subject: str, body: str) -> str:
    """生成 mailto: 链接（由用户本机邮件客户端完成发送，服务端不发送）。"""
    return (
        f"mailto:{quote(to_addr)}"
        f"?subject={quote(subject or '')}"
        f"&body={quote(body or '')}"
    )


def process_send(
    *,
    protocol: str,
    sender: str,
    subject: str,
    recipients: list[EmailRecipient],
    confirm: bool = False,
) -> EmailSendResponse:
    """按协议处理发送请求。**只处理 send=True 的收件人。**

    - draft_only：全部 preview，不发送（默认，Q-02 合规）
    - mailto：返回 mailto 链接，preview（真正发送在用户点开邮件客户端后）
    - smtp：confirm=True 且 enabled 且配置齐全时真实发送，否则 preview / failed
    """
    protocol = (protocol or DEFAULT_EMAIL_PROTOCOL).strip()
    if protocol not in VALID_PROTOCOLS:
        return EmailSendResponse(
            protocol=protocol,
            dry_run=True,
            results=[],
            error_message=f"未知发送协议：{protocol}（可选：{'、'.join(VALID_PROTOCOLS)}）",
        )

    selected = [r for r in recipients if r.send]
    if not selected:
        return EmailSendResponse(
            protocol=protocol,
            dry_run=True,
            results=[],
            error_message="没有勾选任何收件人；请勾选后再操作（默认全部不发送）",
        )

    # ---- draft_only：只预览 ----
    if protocol == "draft_only":
        return EmailSendResponse(
            protocol=protocol,
            dry_run=True,
            results=[
                EmailSendResult(
                    name=r.name,
                    email=r.email,
                    status="preview",
                    detail="草稿模式：未发送。可复制内容或导出 .md 自行发送",
                )
                for r in selected
            ],
            error_message="",
        )

    # ---- mailto：给出链接，不代为发送 ----
    if protocol == "mailto":
        results = []
        for r in selected:
            if not r.email.strip():
                results.append(
                    EmailSendResult(
                        name=r.name,
                        email="",
                        status="skipped",
                        detail="邮箱未填写，无法生成 mailto 链接（邮箱需你手动填写）",
                    )
                )
                continue
            results.append(
                EmailSendResult(
                    name=r.name,
                    email=r.email,
                    status="preview",
                    detail=mailto_link(r.email, subject, r.content),
                )
            )
        return EmailSendResponse(protocol=protocol, dry_run=True, results=results)

    # ---- smtp：真实发送需三重条件 ----
    cfg = smtp_config()

    if not cfg["available"]:
        return EmailSendResponse(
            protocol=protocol,
            dry_run=True,
            results=[
                EmailSendResult(
                    name=r.name,
                    email=r.email,
                    status="failed",
                    detail="SMTP 未配置",
                )
                for r in selected
            ],
            error_message=(
                "SMTP 协议未启用：缺少 "
                + "、".join(cfg["missing"])
                + "。请在 .env 中配置后再试（当前未发送任何邮件）"
            ),
        )

    if not cfg["enabled"]:
        return EmailSendResponse(
            protocol=protocol,
            dry_run=True,
            results=[
                EmailSendResult(
                    name=r.name,
                    email=r.email,
                    status="preview",
                    detail="发送开关未开启（OFFICEFLOW_EMAIL_ENABLE_SEND=0）",
                )
                for r in selected
            ],
            error_message=(
                "真实发送未开启：请将 .env 中 OFFICEFLOW_EMAIL_ENABLE_SEND 设为 1 "
                "（这是为防止误发的显式开关，当前未发送任何邮件）"
            ),
        )

    if not confirm:
        return EmailSendResponse(
            protocol=protocol,
            dry_run=True,
            results=[
                EmailSendResult(
                    name=r.name,
                    email=r.email,
                    status="preview",
                    detail="未确认发送（confirm=false），仅预览",
                )
                for r in selected
            ],
            error_message="请先勾选收件人并显式确认发送（confirm=true）",
        )

    # ---- 真正发送 ----
    results: list[EmailSendResult] = []
    sent = 0
    for r in selected:
        addr = r.email.strip()
        if not r.email_valid:
            results.append(
                EmailSendResult(
                    name=r.name,
                    email=addr,
                    status="skipped",
                    detail="邮箱为空或格式不正确，已跳过（邮箱不会被自动推测）",
                )
            )
            continue
        try:
            _send_one_smtp(cfg, addr, subject, r.content)
            sent += 1
            results.append(
                EmailSendResult(name=r.name, email=addr, status="sent", detail="已发送")
            )
            logger.info("邮件已发送：%s", addr)
        except Exception as exc:  # noqa: BLE001 - 单封失败不影响其它
            results.append(
                EmailSendResult(
                    name=r.name,
                    email=addr,
                    status="failed",
                    detail=f"发送失败：{type(exc).__name__}: {exc}",
                )
            )
            logger.warning("邮件发送失败 %s：%s", addr, exc)

    errors = [r for r in results if r.status == "failed"]
    return EmailSendResponse(
        protocol=protocol,
        dry_run=False,
        results=results,
        error_message=(
            f"{len(errors)} 封发送失败，其余 {sent} 封已发送" if errors else ""
        ),
    )
