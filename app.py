import os
import uuid
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from azure.data.tables import TableServiceClient, UpdateMode
from azure.storage.blob import BlobServiceClient, ContentSettings

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")

CONN_STR = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.environ.get("AZURE_BLOB_CONTAINER", "images")

POSTS_TABLE = "Posts"
USERS_TABLE = "Users"
APPEALS_TABLE = "Appeals"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_table_client(table_name: str):
    if not CONN_STR:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is not set")
    service = TableServiceClient.from_connection_string(conn_str=CONN_STR)
    service.create_table_if_not_exists(table_name=table_name)
    return service.get_table_client(table_name=table_name)


def get_blob_container_client():
    if not CONN_STR:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is not set")
    service = BlobServiceClient.from_connection_string(conn_str=CONN_STR)
    container = service.get_container_client(CONTAINER_NAME)
    try:
        container.create_container()
    except Exception:
        pass
    return container


def ensure_user(username: str):
    username = (username or "学生匿名").strip()[:30]
    users_table = get_table_client(USERS_TABLE)
    try:
        entity = users_table.get_entity(partition_key="USER", row_key=username)
    except Exception:
        entity = {
            "PartitionKey": "USER",
            "RowKey": username,
            "points": 10,
            "posts_count": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        users_table.upsert_entity(entity=entity, mode=UpdateMode.REPLACE)
    return entity


def upload_image_to_blob(file_storage) -> str:
    if not file_storage or not file_storage.filename:
        return ""
    if not allowed_file(file_storage.filename):
        raise ValueError("Unsupported image format. Use png, jpg, jpeg, gif, or webp.")

    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    blob_name = f"{uuid.uuid4().hex}.{ext}"
    container = get_blob_container_client()
    blob_client = container.get_blob_client(blob_name)

    content_type_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }
    blob_client.upload_blob(
        file_storage.read(),
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type_map.get(ext, "application/octet-stream")),
    )
    return blob_client.url


@app.route("/health")
def health():
    return jsonify({"status": "ok", "project": "Seishun Link"})


@app.route("/", methods=["GET"])
def index():
    category = request.args.get("category", "すべて").strip()
    search_q = request.args.get("q", "").strip().lower()
    user_name = request.args.get("user", "").strip()

    posts = []
    user_info = None
    db_error = None

    try:
        posts_table = get_table_client(POSTS_TABLE)
        raw_posts = list(posts_table.query_entities(query_filter="PartitionKey eq 'POST'"))
        for p in raw_posts:
            item = {
                "id": p.get("RowKey"),
                "title": p.get("title", ""),
                "content": p.get("content", ""),
                "category": p.get("category", "お知らせ"),
                "author": p.get("author", "匿名"),
                "image_url": p.get("image_url", ""),
                "likes": int(p.get("likes", 0)),
                "created_at": p.get("created_at", ""),
            }
            if category and category != "すべて" and item["category"] != category:
                continue
            if search_q and search_q not in item["title"].lower() and search_q not in item["content"].lower():
                continue
            posts.append(item)

        posts.sort(key=lambda x: x["created_at"], reverse=True)

        if user_name:
            user_info = ensure_user(user_name)
    except Exception as exc:
        db_error = str(exc)

    categories = ["すべて", "お知らせ", "部活動", "イベント", "質問・相談", "落とし物"]
    return render_template(
        "index.html",
        posts=posts,
        categories=categories,
        selected_category=category,
        search_q=search_q,
        user_name=user_name,
        user_info=user_info,
        db_error=db_error,
    )


@app.route("/posts", methods=["POST"])
def create_post():
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    category = request.form.get("category", "お知らせ").strip()
    author = request.form.get("author", "学生").strip() or "学生"
    image_file = request.files.get("image")

    if not title or not content:
        flash("タイトルと本文を入力してください。", "error")
        return redirect(url_for("index", user=author))

    try:
        image_url = upload_image_to_blob(image_file)
        post_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        posts_table = get_table_client(POSTS_TABLE)
        posts_table.upsert_entity(
            entity={
                "PartitionKey": "POST",
                "RowKey": post_id,
                "title": title[:100],
                "content": content[:2000],
                "category": category,
                "author": author[:30],
                "image_url": image_url,
                "likes": 0,
                "created_at": now_iso,
            },
            mode=UpdateMode.REPLACE,
        )

        users_table = get_table_client(USERS_TABLE)
        user_entity = ensure_user(author)
        user_entity["points"] = int(user_entity.get("points", 0)) + 5
        user_entity["posts_count"] = int(user_entity.get("posts_count", 0)) + 1
        user_entity["updated_at"] = now_iso
        users_table.upsert_entity(entity=user_entity, mode=UpdateMode.REPLACE)

        flash("投稿が完了しました！（+5pt 獲得）", "success")
    except Exception as exc:
        flash(f"投稿に失敗しました: {exc}", "error")

    return redirect(url_for("index", user=author))


@app.route("/posts/<post_id>/like", methods=["POST"])
def like_post(post_id):
    user_name = request.form.get("user", "").strip()
    try:
        posts_table = get_table_client(POSTS_TABLE)
        entity = posts_table.get_entity(partition_key="POST", row_key=post_id)
        entity["likes"] = int(entity.get("likes", 0)) + 1
        posts_table.upsert_entity(entity=entity, mode=UpdateMode.REPLACE)
        flash("いいねしました！", "success")
    except Exception as exc:
        flash(f"いいね処理に失敗しました: {exc}", "error")
    return redirect(url_for("index", user=user_name))


@app.route("/appeals", methods=["POST"])
def create_appeal():
    target_id = request.form.get("target_id", "").strip()
    reason = request.form.get("reason", "").strip()
    reporter = request.form.get("reporter", "匿名").strip() or "匿名"

    if not reason:
        flash("申訴・報告の理由を入力してください。", "error")
        return redirect(url_for("index", user=reporter))

    try:
        appeals_table = get_table_client(APPEALS_TABLE)
        appeal_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        appeals_table.upsert_entity(
            entity={
                "PartitionKey": "APPEAL",
                "RowKey": appeal_id,
                "target_id": target_id or "GENERAL",
                "reason": reason[:1000],
                "reporter": reporter[:30],
                "status": "受付中",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            mode=UpdateMode.REPLACE,
        )
        flash("申訴・通報を受け付けました。", "success")
    except Exception as exc:
        flash(f"送信に失敗しました: {exc}", "error")

    return redirect(url_for("index", user=reporter))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)