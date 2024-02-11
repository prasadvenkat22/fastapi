# import tensorflow as tf
# from PIL import Image
# import numpy as np
# from fastapi import APIRouter

# # router = APIRouter(
# #     prefix = '/ImagePredict',
# #     tags = ['Image Alogos']
# # )

# #MobileNet CNN Model with pre-trained weights 
# #i.e. it is already trained to classify 1000 unique categories of images
# model = None



# def load_model():
#     model = tf.keras.applications.MobileNetV2(weights="imagenet")
#     print("Model loaded")
#     return model
# model = load_model()

# #We define a predict function that will 
# # accept an image and returns the predictions. 
# # We resize the image to 224x224 and normalize the 
# # pixel values to be in [-1, 1].
# from keras.applications.imagenet_utils import decode_predictions
# from io import BytesIO

# #decode_predictions is used to decode the
# #  class name of the predicted object. Here we will return the top-2 probable class.
# def read_imagefile(file) -> Image.Image:
#     image = Image.open(BytesIO(file))
#     return image
# def predict(image: Image.Image):
#     image = np.asarray(image.resize((224, 224)))[..., :3]
#     image = np.expand_dims(image, 0)
#     image = image / 127.5 - 1.0
#     result = decode_predictions(model.predict(image), 2)[0]
#     response = []
#     for i, res in enumerate(result):
#         resp = {}
#         resp["class"] = res[1]
#         resp["confidence"] = f"{res[2]*100:0.2f} %"
#         response.append(resp)
#     return response

