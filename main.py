from fastapi import FastAPI, File, UploadFile, Form
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired
import uvicorn
import shutil
import os
import json
import requests
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pathlib import Path
import aiohttp
import time
import random
import asyncio
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, String, Integer
from fastapi import Query

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
SESSIONS_DIR = "sessions"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL",
                         "postgresql://autoinstauser:0D3LfwDSKrSJC2BAuy5K57PCS8xYqX1l@dpg-d0fs42q4d50c73f80u3g-a:5432/autoinstadb")

database = Database(DATABASE_URL)
metadata = MetaData()

posts_table = Table("posts", metadata, Column("id", Integer, primary_key=True),
                    Column("username", String, nullable=False),
                    Column("password", String, nullable=False))

engine = create_engine(DATABASE_URL)
metadata.create_all(engine)


@app.on_event("startup")
async def startup():
    await database.connect()


@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

@app.get("/posts_ids")
async def get_post_ids():
    query = posts_table.select().with_only_columns(posts_table.c.id)
    rows = await database.fetch_all(query)
    return {"post_ids": [row["id"] for row in rows]}

def get_client(username: str) -> Client:
    cl = Client()
    session_file = Path(SESSIONS_DIR) / f"{username}.json"
    if session_file.exists():
        cl.load_settings(session_file)
    return cl


def save_client_session(cl: Client, username: str):
    session_file = os.path.join(SESSIONS_DIR, f"{username}.json")
    settings = cl.get_settings()
    with open(session_file, "w") as f:
        json.dump(settings, f)


@app.get("/")
async def read_root():
    return {"message": "Welcome to the Instagram API"}


@app.post("/add_post_account")
async def add_post_account(username: str = Query(...),
                           password: str = Query(...)):
    query = posts_table.insert().values(username=username, password=password)
    await database.execute(query)
    return {"status": "success", "message": "Account added for posting"}


@app.get("/auto_post")
async def auto_post():
    await get_send_posts()
    return {"status": "success"}


async def get_send_posts():
    query = posts_table.select()
    posts = await database.fetch_all(query)

    for post in posts:
        username = post["username"]
        password = post["password"]

        try:
            description = await generate_description()
            caption = await generate_caption(description)
            image_path = await generate_image(description)

            cl = get_client(username)
            cl.login(username, password)
            cl.photo_upload(image_path, caption)

            print(f"✅ Post publié pour {username}")
            os.remove(image_path)
        except Exception as e:
            print(f"❌ Échec pour {username} : {e}")


async def generate_description():
    seed = random.randint(1000, 999999)
    prompt = "Generate a description for an AI image generator."
    encoded_prompt = requests.utils.quote(prompt)
    url = f"https://text.pollinations.ai/{encoded_prompt}?model=openai&system=ia%20texte%20generator%20for%20image%20generator&private=true&seed=${seed}"

    retries = 3
    for _ in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise Exception(f"Text API error: {resp.status}")
                    return await resp.text()
        except Exception as e:
            print(f"❌ Erreur lors de la génération de la description : {e}")
            await asyncio.sleep(2)
    return "Erreur lors de la génération de la description après plusieurs tentatives."


async def generate_caption(description):
    seed = random.randint(1000, 999999)
    prompt = f"""Based on the following image description: "{description}", write a short, creative, and unique Instagram caption. Include relevant and diverse hashtags. Avoid repetition or overly generic phrases. Make it feel natural, artistic, fun, or inspiring depending on the content. Each response must be different."""
    encoded_prompt = requests.utils.quote(prompt)
    url = f"https://text.pollinations.ai/{encoded_prompt}?model=openai&system=ia%20helper%20post%20caption%20generator%20for%20Instagram&private=true&seed=${seed}"

    retries = 3
    for _ in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise Exception(f"Text API error: {resp.status}")
                    return await resp.text()
        except Exception as e:
            print(f"❌ Erreur lors de la génération de la description : {e}")
            await asyncio.sleep(2)
    return "Erreur lors de la génération de la description après plusieurs tentatives."


async def generate_image(description):
    seed = random.randint(1000, 999999)
    encoded_prompt = requests.utils.quote(description)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?model=openai&width=512&height=512&seed={seed}&nologo=true&private=true&enhance=true&safe=false"

    for attempt in range(2):
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    image_data = await resp.read()
                    image_path = os.path.join(UPLOAD_DIR,
                                              f"{int(time.time())}.jpg")
                    with open(image_path, "wb") as f:
                        f.write(image_data)
                    return image_path
                elif attempt == 0:
                    print(
                        "⚠️ Première tentative échouée, nouvelle tentative...")
                    await asyncio.sleep(2)
                else:
                    raise Exception(f"Image API error: {resp.status}")


@app.post("/test_login")
async def test_instagram_login(username: str = Form(...),
                               password: str = Form(...)):
    cl = get_client(username)

    def challenge_handler(username, challenge):
        return "email"

    cl.challenge_code_handler = challenge_handler

    try:
        cl.login(username, password)
        save_client_session(cl, username)
        return {"status": "success", "message": "Login successful"}
    except ChallengeRequired:
        return {"status": "error", "message": "challenge_required"}
    except Exception as e:
        return {"status": "error", "message": f"Login failed: {str(e)}"}


@app.post("/verify_challenge")
async def verify_challenge(username: str = Form(...), code: str = Form(...)):
    cl = get_client(username)
    try:
        result = cl.challenge_code_verify(code)
        save_client_session(cl, username)
        return {
            "status": "success",
            "message": "Challenge verified and session saved"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Challenge verification failed: {str(e)}"
        }


@app.post("/upload")
async def upload_instagram_post(caption: str = Form(...),
                                username: str = Form(...),
                                password: str = Form(...),
                                file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".jpg", ".jpeg", ".png")):
        return {
            "status": "error",
            "message": "Only JPG/JPEG/PNG images are supported."
        }

    cl = get_client(username)
    try:
        cl.login(username, password)
        save_client_session(cl, username)
    except ChallengeRequired:
        return {"status": "error", "message": "challenge_required"}
    except Exception as e:
        return {"status": "error", "message": f"Login failed: {str(e)}"}

    file_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        media = cl.photo_upload(file_path, caption)
    except Exception as e:
        os.remove(file_path)
        return {"status": "error", "message": f"Upload failed: {str(e)}"}

    os.remove(file_path)
    return {"status": "success", "media_id": media.pk}


@app.post("/get_total_stats")
async def get_total_stats(username: str = Form(...),
                          password: str = Form(...)):
    cl = get_client(username)
    try:
        cl.login(username, password)
    except Exception as e:
        return {"status": "error", "message": f"Login failed: {str(e)}"}

    try:
        user_id = cl.user_id_from_username(username)
        medias = cl.user_medias(user_id, amount=50)

        total_likes = sum(m.like_count for m in medias)
        total_views = sum(m.view_count for m in medias if m.media_type == 2)
        total_comments = sum(len(cl.media_comments(m.pk)) for m in medias)

        return {
            "status": "success",
            "total_likes": total_likes,
            "total_views": total_views,
            "total_comments": total_comments,
            "total_posts": len(medias)
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to fetch stats: {str(e)}"
        }


@app.post("/dashboard")
async def get_instagram_posts(username: str = Form(...),
                              password: str = Form(...),
                              amount: int = Form(5)):
    cl = get_client(username)
    try:
        cl.login(username, password)
    except Exception as e:
        return {"status": "error", "message": f"Login failed: {str(e)}"}

    user_id = cl.user_id_from_username(username)
    medias = cl.user_medias(user_id, amount)

    posts_data = []
    for media in medias:
        post = {
            "media_id":
            cl.media_id(media.pk),
            "image_url":
            media.thumbnail_url,
            "caption":
            media.caption_text,
            "like_count":
            media.like_count,
            "timestamp":
            media.taken_at.isoformat() if media.taken_at else None,
            "is_video":
            media.media_type == 2,
            "view_count":
            getattr(media, "view_count", None)
            if media.media_type == 2 else None,
            "comments": [{
                "user": c.user.username,
                "text": c.text
            } for c in cl.media_comments(media.pk, amount=5)]
        }
        posts_data.append(post)

    return {"status": "success", "posts": posts_data}


@app.get("/image/{image_url:path}")
async def get_image(image_url: str):
    try:
        response = requests.get(image_url, stream=True)
        return StreamingResponse(response.iter_content(chunk_size=1024),
                                 media_type="image/jpeg")
    except requests.RequestException as e:
        return {
            "status": "error",
            "message": f"Failed to fetch image: {str(e)}"
        }


@app.post("/reply_comment")
async def reply_to_comment(username: str = Form(...),
                           password: str = Form(...),
                           media_id: str = Form(...),
                           comment_id: int = Form(...),
                           reply_text: str = Form(...)):
    cl = get_client(username)
    try:
        cl.login(username, password)
    except Exception as e:
        return {"status": "error", "message": f"Login failed: {str(e)}"}

    try:
        cl.comment_reply(media_id, comment_id, reply_text)
        return {"status": "success", "message": "Reply sent successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Reply failed: {str(e)}"}


@app.post("/get_info_post")
async def get_info_post(username: str = Form(...),
                        password: str = Form(...),
                        postId: str = Form(...)):
    cl = get_client(username)
    try:
        cl.login(username, password)
    except Exception as e:
        return {"status": "error", "message": f"Login failed: {str(e)}"}

    try:
        media = cl.media_info(postId)
        comments = cl.media_comments(media.pk, amount=5)
        return {
            "status":
            "success",
            "media_id":
            media.pk,
            "image_url":
            media.thumbnail_url,
            "caption":
            media.caption_text,
            "like_count":
            media.like_count,
            "timestamp":
            media.taken_at.isoformat() if media.taken_at else None,
            "is_video":
            media.media_type == 2,
            "view_count":
            getattr(media, "view_count", None)
            if media.media_type == 2 else None,
            "comments": [{
                "id": c.pk,
                "user": c.user.username,
                "text": c.text
            } for c in comments]
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to fetch post: {str(e)}"
        }


@app.post("/comment_post")
async def comment_on_post(username: str = Form(...),
                          password: str = Form(...),
                          media_id: str = Form(...),
                          comment_text: str = Form(...)):
    cl = get_client(username)
    try:
        cl.login(username, password)
    except Exception as e:
        return {"status": "error", "message": f"Login failed: {str(e)}"}

    try:
        cl.media_comment(media_id, comment_text)
        return {"status": "success", "message": "Comment posted successfully"}
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to post comment: {str(e)}"
        }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
