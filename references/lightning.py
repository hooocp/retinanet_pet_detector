from logging import log
import os
from typing import Dict, Union

import pandas as pd
import pytorch_lightning as pl
import torch
from omegaconf import DictConfig
from pytorch_lightning.callbacks import (
    EarlyStopping,
    LearningRateLogger,
    ModelCheckpoint,
)
from torch import nn
from torch.utils.data import DataLoader

from pytorch_retinanet.utils import CocoEvaluator, get_coco_api_from_dataset
from pytorch_retinanet import DetectionDataset
from pytorch_retinanet.retinanet.utilities import collate_fn

from .data_utils import _get_logger
from .utils import get_tfms, load_obj


class DetectionModel(pl.LightningModule):
    """
    Lightning Class to wrap the RetinaNet Model.
    So that it can be trainer with LightningTrainer.

    Args:
      model (`nn.Module`)    : A `RetinaNet` model instance. 
      haprams (`DictConfig`) : A `DictConfig` that stores the configs for training .
                               Check `main.yaml` in the parent dir.
    """

    def __init__(self, model: nn.Module, hparams: DictConfig):
        super(DetectionModel, self).__init__()
        self.model = model
        self.hparams = hparams
        # instantiate logger
        self.fancy_logger = _get_logger(__name__)

    # ===================================================== #
    # Configure the Optimizer & Scheduler for the Model
    # ===================================================== #
    def configure_optimizers(self, *args, **kwargs):
        "instatiates optimizer & scheduler(s)"
        params = [p for p in self.model.parameters() if p.requires_grad]
        # intialize optimizer
        self.optimizer = load_obj(self.hparams.optimizer.class_name)(params, **self.hparams.optimizer.params)

        # initialize scheduler
        self.scheduler = load_obj(self.hparams.scheduler.class_name)(self.optimizer, **self.hparams.scheduler.params)
        self.scheduler = {
            "scheduler": self.scheduler,
            "interval": self.hparams.scheduler.interval,
            "frequency": self.hparams.scheduler.frequency,
        }

        # log optimizer and scheduler
        self.fancy_logger.info(f"Optimizer : {self.optimizer.__class__.__name__}")
        self.fancy_logger.info(f"Scheduler : {self.scheduler['scheduler'].__class__.__name__}")
        return [self.optimizer], [self.scheduler]

    # ===================================================== #
    # Getting the data ready
    # ===================================================== #
    def prepare_data(self, stage=None):
        """
        load in the transformation & reads in the data from given paths.
        """
        # instantiate the transforms
        self.tfms = get_tfms(self.hparams)
        _augs = self.tfms["train"].transforms
        prompt = [_augs[i].__class__.__name__ for i in range(len(list(_augs)))]
        self.fancy_logger.info(f"Augmentations used in training: {prompt}")

        # load in the csv files
        # train csv
        self.trn_df = pd.read_csv(self.hparams.train_csv)
        self.fancy_logger.info(f"Serialized train dataset from {self.hparams.train_csv}")
        self.fancy_logger.info(f"Serialized dataset takes {os.path.getsize(self.hparams.train_csv)/(1024*1024):.2f} MiB")
        # validation csv file
        self.val_df = pd.read_csv(self.hparams.valid_csv)
        self.fancy_logger.info(f"Serialized validation dataset from {self.hparams.valid_csv}")
        self.fancy_logger.info(f"Serialized dataset takes {os.path.getsize(self.hparams.valid_csv)/(1024*1024):.2f} MiB")
        # test csv file
        self.test_df = pd.read_csv(self.hparams.test_csv)
        self.fancy_logger.info(f"Serialized test dataset from {self.hparams.test_csv}")
        self.fancy_logger.info(f"Serialized dataset takes {os.path.getsize(self.hparams.test_csv)/(1024*1024):.2f} MiB")

    # ===================================================== #
    # Forward pass of the Model
    # ===================================================== #
    def forward(self, xb, *args, **kwargs):
        return self.model(xb)

    # ===================================================== #
    # Training
    # ===================================================== #
    def train_dataloader(self, *args, **kwargs):
        # instantiate the trian dataset
        train_ds = DetectionDataset(self.trn_df, self.tfms["train"])
        # load in the dataloader
        bs = self.hparams.train_batch_size
        trn_dl = DataLoader(train_ds, bs, True, collate_fn=collate_fn, **self.hparams.dataloader)
        return trn_dl

    def training_step(self, batch, batch_idx, *args, **kwargs):
        images, targets, _ = batch  # unpack the one batch from the DataLoader
        targets = [{k: v for k, v in t.items()} for t in targets]  # Unpack the Targets
        # Calculate Losses {regression_loss , classification_loss}
        loss_dict = self.model(images, targets)
        # Calculate Total Loss
        losses = sum(loss for loss in loss_dict.values())
        return {"loss": losses, "log": loss_dict, "progress_bar": loss_dict}

    # ===================================================== #
    # Validation
    # ===================================================== #
    def val_dataloader(self, *args, **kwargs):
        # instantiate the validaiton dataset
        val_ds = DetectionDataset(self.val_df, self.tfms["valid"])
        # instantiate dataloader
        bs = self.hparams.valid_batch_size
        loader = DataLoader(val_ds, bs, collate_fn=collate_fn, **self.hparams.dataloader)
        return loader

    def validation_step(self, batch, batch_idx, *args, **kwargs):
        images, targets, _ = batch  # unpack the one batch from the DataLoader
        targets = [{k: v for k, v in t.items()} for t in targets]  # Unpack the Targets
        # Calculate Losses {regression_loss , classification_loss}
        loss_dict = self.model(images, targets)
        # Calculate Total Loss
        loss = sum(loss for loss in loss_dict.values())
        result = pl.EvalResult()
        # log metrics for each validation_step, and the average across the epoch, to the progress bar and logger
        result.log('val_loss', loss, on_step=False, on_epoch=True, prog_bar=True, logger=True)
        return result


    # ===================================================== #
    # Test
    # ===================================================== #
    def test_dataloader(self, *args, **kwargs):
        # instantiate train dataset
        test_ds = DetectionDataset(self.test_df, self.tfms["test"])
        # instantiate dataloader
        bs = self.hparams.test_batch_size
        loader = DataLoader(test_ds, bs, collate_fn=collate_fn, **self.hparams.dataloader)
        # instantiate coco_api to track metrics
        coco = get_coco_api_from_dataset(loader.dataset)
        self.test_evaluator = CocoEvaluator(coco, [self.hparams.iou_types])
        return loader

    def test_step(self, batch, batch_idx, *args, **kwargs):
        images, targets, _ = batch
        targets = [{k: v for k, v in t.items()} for t in targets]
        outputs = self.model.predict(images)
        res = {t["image_id"].item(): o for t, o in zip(targets, outputs)}
        self.test_evaluator.update(res)
        return {}

    def test_epoch_end(self, outputs, *args, **kwargs):
        # coco results
        self.fancy_logger.info("Evaluation results: ")
        self.test_evaluator.accumulate()
        self.test_evaluator.summarize()
        metric = self.test_evaluator.coco_eval["bbox"].stats[0]
        metric = torch.as_tensor(metric)
        logs = {"AP": metric}
        return {"AP": metric, "log": logs, "progress_bar": logs,}


class LogCallback(pl.Callback):
    """
    Callback to handle logging within pl_module
    """

    def on_train_start(self, trainer, pl_module):
        prompt = f"Training on {pl_module.train_dataloader().dataset.__len__()} images"
        pl_module.fancy_logger.info(prompt)
        prompt = f"Training from iteration {trainer.global_step} : "
        pl_module.fancy_logger.info(prompt)

    def on_test_start(self, trainer, pl_module):
        prompt = f"Inference on {pl_module.test_dataloader().dataset.__len__()} images"
        pl_module.fancy_logger.info(prompt)


def initialize_trainer(trainer_conf: Union[DictConfig, Dict], **kwargs) -> pl.Trainer:
    """
    Instantiates a Lightning Trainer from given config file .
    The Trainer is initialized with the flags given in the config
    file with the addition of the `LogCallback`.

    Args:
        trainer_conf `(DictConfig)`: configs for the Trainer.
        **kwargs – Other arguments are passed directly to the `pl.Trainer`.
    """
    # instantiate EarlyStoppping Callback
    early_stopping = EarlyStopping(**trainer_conf.early_stopping.params)

    # instantiate ModelCheckpoint Callback
    os.makedirs(trainer_conf.model_checkpoint.params.filepath, exist_ok=True)
    model_checkpoint = ModelCheckpoint(**trainer_conf.model_checkpoint.params)

    # instantiate callbacks
    lr_logger = LearningRateLogger(**trainer_conf.learning_rate_monitor.params)
    logger = load_obj(trainer_conf.logger.class_name)(**trainer_conf.logger.params)
    log_cb = LogCallback()

    callbacks = [lr_logger, log_cb]
    logger = [logger]

    # Load Trainer:
    trainer = pl.Trainer(
        logger=logger,
        checkpoint_callback=model_checkpoint,
        early_stop_callback=early_stopping,
        callbacks=callbacks,
        **trainer_conf.flags,
        **kwargs,
    )

    return trainer
