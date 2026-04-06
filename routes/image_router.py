import os
import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from config.db_pgrs import SessionLocal
import models_pgdb.models as models

router = APIRouter(prefix="/images", tags=["Image Uploads"])

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB
STATIC_DIR = "static"

ENTITY_MAP = {
    "user":     models.User,
    "customer": models.Customer,
    "device":   models.Device,
    "service":  models.Service,
    "invoice":  models.Invoice,
    "product":  models.Product,
}

EXTENSION_MAP = {
    "image/jpeg": "jpg",
    "image/png":  "png",
    "image/webp": "webp",
}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


db_dependency = Annotated[Session, Depends(get_db)]


@router.post("/upload", status_code=status.HTTP_200_OK)
async def upload_image(entity: str, id: int, file: UploadFile, db: db_dependency):
    """Upload an image for any entity. Saves to static/{entity}/{id}.{ext} and stores the URL in DB."""

    if entity not in ENTITY_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown entity '{entity}'. Valid options: {list(ENTITY_MAP.keys())}",
        )

    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{file.content_type}'. Allowed: jpeg, png, webp",
        )

    # Check file size
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size > MAX_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds 2 MB limit")

    model_class = ENTITY_MAP[entity]
    record = db.query(model_class).filter(model_class.id == id).first()
    if not record:
        raise HTTPException(status_code=404, detail=f"{entity.capitalize()} with id {id} not found")

    ext = EXTENSION_MAP[file.content_type]
    folder = os.path.join(STATIC_DIR, f"{entity}s")
    os.makedirs(folder, exist_ok=True)

    filename = f"{id}.{ext}"
    file_path = os.path.join(folder, filename)

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    image_url = f"/static/{entity}s/{filename}"
    record.image_url = image_url
    db.commit()

    return {"entity": entity, "id": id, "image_url": image_url}


@router.delete("/{entity}/{id}", status_code=status.HTTP_200_OK)
async def delete_image(entity: str, id: int, db: db_dependency):
    """Delete the image for an entity — removes the file and clears image_url in DB."""

    if entity not in ENTITY_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown entity '{entity}'. Valid options: {list(ENTITY_MAP.keys())}",
        )

    model_class = ENTITY_MAP[entity]
    record = db.query(model_class).filter(model_class.id == id).first()
    if not record:
        raise HTTPException(status_code=404, detail=f"{entity.capitalize()} with id {id} not found")

    if not record.image_url:
        raise HTTPException(status_code=404, detail=f"No image found for {entity} id {id}")

    # Remove file from disk
    file_path = record.image_url.lstrip("/")
    if os.path.exists(file_path):
        os.remove(file_path)

    record.image_url = None
    db.commit()

    return {"entity": entity, "id": id, "image_url": None}


@router.get("/{entity}/{id}", status_code=status.HTTP_200_OK)
async def get_image_url(entity: str, id: int, db: db_dependency):
    """Return the image URL for a given entity record."""

    if entity not in ENTITY_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown entity '{entity}'. Valid options: {list(ENTITY_MAP.keys())}",
        )

    model_class = ENTITY_MAP[entity]
    record = db.query(model_class).filter(model_class.id == id).first()
    if not record:
        raise HTTPException(status_code=404, detail=f"{entity.capitalize()} with id {id} not found")

    if not record.image_url:
        raise HTTPException(status_code=404, detail=f"No image found for {entity} id {id}")

    return {"entity": entity, "id": id, "image_url": record.image_url}
