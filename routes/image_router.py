import os
import shutil
import uuid
from typing import Annotated, List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from config.db_pgrs import SessionLocal
import models_pgdb.models as models
from schemas_pgrs.schema import EntityImageResponse

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


@router.post("/gallery/upload", response_model=EntityImageResponse, status_code=status.HTTP_201_CREATED)
async def upload_gallery_image(entity: str, id: int, file: UploadFile, db: db_dependency, sort_order: int = 0):
    """Add one image to an entity's gallery. Call this repeatedly to build a multi-photo
    gallery (e.g. several product/property photos) — unlike /images/upload, this never
    overwrites a previous image; each upload is its own row."""

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

    filename = f"{id}_{uuid.uuid4().hex}.{ext}"
    file_path = os.path.join(folder, filename)

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    image = models.EntityImage(
        entity_type=entity,
        entity_id=id,
        image_url=f"/static/{entity}s/{filename}",
        sort_order=sort_order,
    )
    db.add(image)
    db.commit()
    db.refresh(image)

    return image


@router.get("/gallery/{entity}/{id}", response_model=List[EntityImageResponse])
async def list_gallery_images(entity: str, id: int, db: db_dependency):
    """Return every gallery image for an entity record, ordered by sort_order."""

    if entity not in ENTITY_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown entity '{entity}'. Valid options: {list(ENTITY_MAP.keys())}",
        )

    return (
        db.query(models.EntityImage)
        .filter(models.EntityImage.entity_type == entity, models.EntityImage.entity_id == id)
        .order_by(models.EntityImage.sort_order, models.EntityImage.created_at)
        .all()
    )


@router.delete("/gallery/{image_id}", status_code=status.HTTP_200_OK)
async def delete_gallery_image(image_id: int, db: db_dependency):
    """Delete a single gallery image by its own id — removes the file and the row."""

    image = db.query(models.EntityImage).filter(models.EntityImage.id == image_id).first()
    if not image:
        raise HTTPException(status_code=404, detail=f"Gallery image with id {image_id} not found")

    file_path = image.image_url.lstrip("/")
    if os.path.exists(file_path):
        os.remove(file_path)

    db.delete(image)
    db.commit()

    return {"id": image_id, "deleted": True}


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
