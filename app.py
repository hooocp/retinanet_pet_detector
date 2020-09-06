import argparse
import warnings
from typing import *

import numpy as np
import streamlit as st
from PIL import Image

from display_preds import Visualizer
from inference import load_yaml_config
from utils import get_model
from utils import get_predictions_v2 as get_preds
from utils import label_dict

warnings.filterwarnings("ignore")

DEVICE = "cpu"
# Instantiate the visualizer
viz = Visualizer(label_dict)


# load model with @st.cache so it doesn't take a long time each time
@st.cache(allow_output_mutation=True)
def load_model(args: argparse.Namespace):
    " loads in the pre-trained RetinaNet Model "
    with st.slider("Serializing Model ..."):
        model = get_model(args)
        model.to(device=DEVICE)
    return model


def load_image():
    uploaded_image = st.file_uploader(
        "Choose a png or jpg image", type=["jpg", "png", "jpeg"]
    )
    if uploaded_image is not None:
        st.markdown("## Uploaded Image")
        # Make sure image is RGB
        image = Image.open(uploaded_image).convert("RGB")
        # Convert PIL image to array
        image = np.array(image)
        # Display the uploaded Image
        st.image(image, use_column_width=True)
        return image


def draw_preds_on_image(
    uploaded_image: np.array,
    boxes: Union[List, np.array],
    labels: Union[List, np.array],
    scores: Union[List, np.array],
):
    image = viz.draw_bboxes(
        uploaded_image, boxes, labels, scores, save=False, show=False, return_fig=True
    )
    return image


def start_app() -> None:
    st.title("Detecting Pet Faces 👁")
    st.write(
        "This application detects the faces of some common Pet Breeds using a [RetinaNet](https://github.com/benihime91/pytorch_retinanet)."
    )
    st.write("## How does it work?")
    st.write(
        "Add an image of a pet (cat or dog) and the app will draw the dounding box where it detects the objects:"
    )
    st.image(
        Image.open("app_images/res_1.png"),
        caption="Example of model being on the Image of a dog [breed: leonburger].",
        use_column_width=True,
    )
    st.write("## Upload your own image")
    st.write(
        "**Note:** The model has been trained on pets breeds given in the [The Oxford-IIIT Pet Dataset](https://www.robots.ox.ac.uk/~vgg/data/pets/)"
        " and therefore will only with those kind of images."
    )
    st.markdown(
        "**To be more precise the model has been trained on these breeds:**")
    st.image(
        Image.open("app_images/breed_count.jpg"),
        caption="Train Data Statistics ",
        use_column_width=True,
    )
    st.markdown("[credits](https://www.robots.ox.ac.uk/~vgg/data/pets/)")


def main(args: argparse.Namespace):
    start_app()
    image = load_image()
    model = get_model(args)
    st.markdown("> Detection Parameters")
    score_threshold = st.slider(
        label="score threshold for detections (Detections with score < score_threshold are discarded)",
        min_value=0.1,
        max_value=1.0,
        value=0.5,
    )

    iou_threshold = st.slider(
        label="iou threshold for detection bounding boxes",
        min_value=0.1,
        max_value=1.0,
        value=0.2,
    )

    # If image is Upladed make predictions when button is clicked
    if image is not None:
        if st.button("Generate predictions"):

            with st.spinner("Generating results ... "):
                # Get instance predictions for the uploaded Image
                bb, lb, sc = get_preds(
                    model, image, score_threshold, iou_threshold,)

            st.markdown("## Results")
            st.markdown(
                f"> - **Number of bounding boxes detected in Image** : {len(bb)}\n"
                f"- **Breeds detected in Image** : {[label_dict[idx] for idx in lb]}"
            )

            with st.spinner("Writing results on the Image:"):
                # Draw on predictions to image
                res = draw_preds_on_image(image, bb, lb, sc)
                st.write(res)


if __name__ == "__main__":
    # load in the args using the config file
    args = load_yaml_config(path='config.yaml')
    # run the app
    main(args)
