from fastapi import FastAPI, File, UploadFile, Form, Query
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired
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
from sqlalchemy import create_engine, MetaData, Table, Column, String, Integer, Text, select, update, insert
from sqlalchemy.dialects.postgresql import insert

from concurrent.futures import ThreadPoolExecutor
import datetime

executor = ThreadPoolExecutor()
pending_challenges = {}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://autoinstauser:0D3LfwDSKrSJC2BAuy5K57PCS8xYqX1l@dpg-d0fs42q4d50c73f80u3g-a:5432/autoinstadb"
)

database = Database(DATABASE_URL)
metadata = MetaData()

posts_table = Table("new_cron", metadata, Column("id", Integer, primary_key=True),
                    Column("username", String, nullable=False, unique=True),
                    Column("password", String, nullable=False),
                    Column("time", String, nullable=False),
                    Column("cron_time", String, nullable=False),)

sessions_table = Table(
    "cookie_sessions",
    metadata,
    Column("username", String, primary_key=True),
    Column("cookie", Text),
)

engine = create_engine(DATABASE_URL)
metadata.create_all(engine)

@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

async def get_client(username: str, password: str) -> Client:
    cl = Client()
    query = sessions_table.select().where(sessions_table.c.username == username)
    row = await database.fetch_one(query)
    loop = asyncio.get_event_loop()

    if row and row["cookie"]:
        try:
            print("[INFO] Chargement des cookies depuis la base")
            cl.set_settings(json.loads(row["cookie"]))
            await loop.run_in_executor(executor, cl.account_info)
            print("[INFO] Cookies valides, connexion sans login")
            return cl
        except LoginRequired:
            print("[WARNING] Cookies expirés, suppression en base")
        except Exception as e:
            print(f"[ERROR] Erreur lors de l'utilisation des cookies : {e}")

        delete_query = sessions_table.delete().where(sessions_table.c.username == username)
        await database.execute(delete_query)

    try:
        print("[INFO] Connexion manuelle...")
        await loop.run_in_executor(executor, cl.login, username, password)
    except ChallengeRequired:
        print("[INFO] Challenge requis, attente de vérification")
        pending_challenges[username] = cl
        raise
    except Exception as e:
        print(f"[ERROR] Échec du login : {e}")
        raise

    cookie_json = json.dumps(cl.get_settings())
    insert_query = sessions_table.insert().values(username=username, cookie=cookie_json)
    await database.execute(insert_query)
    print("[INFO] Connexion réussie, cookies sauvegardés")

    return cl

@app.get("/")
async def read_root():
    return {"message": "Welcome to the Instagram API"}

@app.post("/set_cron")
async def add_cron_account(username: str = Query(...),
                           password: str = Query(...),
                           time: str = Query(...),
                           cron_time: str = Query(...)):
    stmt = insert(posts_table).values(
        username=username,
        password=password,
        time=time,
        cron_time=cron_time
    ).on_conflict_do_update(
        index_elements=["username"],
        set_={
            "password": password,
            "time": time,
            "cron_time": cron_time
        }
    )
    await database.execute(stmt)
    return {"status": "success", "message": "Account added or updated for posting"}

@app.get("/posts_ids")
async def get_post_ids():
    query = select(posts_table.c.id)
    rows = await database.fetch_all(query)
    return {"post_ids": [row["id"] for row in rows]}

@app.get("/auto_post")
async def auto_post():
    await get_send_posts()
    return {"status": "success"}

def parse_cron_time(cron_time_str):
    parts = cron_time_str.split(':')
    if len(parts) != 2:
        raise ValueError(f"Invalid cron_time format: {cron_time_str}")
    hours = int(parts[0])
    minutes = int(parts[1])
    return hours * 60 + minutes

def should_post(post):
    start_time_str = post["time"]       
    interval_minutes = parse_cron_time(post["cron_time"])

    now = datetime.datetime.now()

    start_hour, start_minute = map(int, start_time_str.split(":"))
    start_time = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)

    if start_time > now:
        start_time -= datetime.timedelta(days=1)

    minutes_since_start = int((now - start_time).total_seconds() // 60)

    return minutes_since_start % interval_minutes == 0


async def get_send_posts():
    query = posts_table.select()
    posts = await database.fetch_all(query)

    for post in posts:
        username = post["username"]
        password = post["password"]

        if should_post(post):
            try:
                description = await generate_description()
                caption = await generate_caption(description)
                image_path = await generate_image(description)

                cl = await get_client(username, password)
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
async def test_instagram_login(username: str = Form(...), password: str = Form(...)):
    try:
        cl = await get_client(username, password)
        user_info = cl.account_info()

        if username in pending_challenges:
            del pending_challenges[username]

        return {
            "status": "success",
            "message": "Login successful",
            "user": {
                "username": user_info.username,
                "full_name": user_info.full_name,
                "pk": user_info.pk,
                "profile_pic_url": user_info.profile_pic_url
            }
        }
    except ChallengeRequired:
        cl = Client()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(executor, cl.login, username, password)
        pending_challenges[username] = cl

        return {
            "status": "challenge_required",
            "message": "Instagram requires verification code. Please submit it using /verify_challenge."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/verify_challenge")
async def verify_challenge(username: str = Form(...), code: str = Form(...)):
    cl = pending_challenges.get(username)
    if not cl:
        return {"status": "error", "message": "No pending challenge found for this user."}

    try:
        cl.challenge_code_handler = lambda: code

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(executor, cl.challenge_resolve)

        cookie_json = json.dumps(cl.get_settings())
        delete_query = sessions_table.delete().where(sessions_table.c.username == username)
        await database.execute(delete_query)
        insert_query = sessions_table.insert().values(username=username, cookie=cookie_json)
        await database.execute(insert_query)

        del pending_challenges[username]

        return {"status": "success", "message": "Challenge passed, login complete."}
    except Exception as e:
        return {"status": "error", "message": f"Challenge failed: {e}"}
        
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

    cl = await get_client(username, password)

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
    cl = await get_client(username, password)

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
    cl = await get_client(username, password)

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
    cl = await get_client(username, password)

    try:
        cl.comment_reply(media_id, comment_id, reply_text)
        return {"status": "success", "message": "Reply sent successfully"}
    except Exception as e:
        return {"status": "error", "message": f"Reply failed: {str(e)}"}


@app.post("/get_info_post")
async def get_info_post(username: str = Form(...),
                        password: str = Form(...),
                        postId: str = Form(...)):
    cl = await get_client(username, password)

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
    cl = await get_client(username, password)

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
