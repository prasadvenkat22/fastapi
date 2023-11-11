from fastapi import  File, UploadFile
from starlette.responses import RedirectResponse
from .image_train_router import predict, read_imagefile
from fastapi import APIRouter

router = APIRouter(
    prefix = '/Machine Learning',
    tags = ['Images']
)

@router.post("/predict/image")
async def predict_api(file: UploadFile = File(...)):
    extension = file.filename.split(".")[-1] in ("jpg", "jpeg", "png")
    if not extension:
        return "Image must be jpg or png format!"
    image = read_imagefile(await file.read())
    prediction = predict(image)

    return prediction