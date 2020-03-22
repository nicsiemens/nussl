import ignite
from ignite.engine import Events, Engine
from ignite.handlers import Timer
from torch.utils.tensorboard import SummaryWriter
import torch
from torch import nn
import logging
import time
from enum import Enum
import os
import numpy as np
from nussl import STFTParams

class ValidationEvents(Enum):
    """
    Events based on validation running
    """
    VALIDATION_STARTED = 'validation_started'
    VALIDATION_COMPLETED = 'validation_completed'

class BackwardsEvents(Enum):
    """
    Events based on validation running
    """
    BACKWARDS_COMPLETED = 'backwards_completed'

def cache_dataset(dataset, log_frequency=.1):
    """
    Runs through an entire dataset and caches it if there nussl.datasets.transforms.Cache
    is in dataset.transform. If there is no caching, or dataset.cache_populated = True,
    then this function just iterates through the dataset and does nothing.

    This function can also take a `torch.util.data.DataLoader` object wrapped around
    a `nussl.datasets.BaseDataset` object.
    
    Args:
        dataset (nussl.datasets.BaseDataset): Must be a subclass of 
          `nussl.datasets.BaseDataset`.

        log_frequency (float, optional): How often to log progress, as a fraction between
          0.0 and 1.0 of the total dataset length. Defaults to .1 
          (10x over the course of caching).
    """
    def dummy_process(engine, data):
        pass

    cache = Engine(dummy_process)
    log_frequency = max(int(len(dataset) * log_frequency), 1)

    @cache.on(Events.ITERATION_STARTED(every=log_frequency))
    def log_progress(engine):
        logging.info(
            f"Cached {engine.state.iteration} / "
            f"{engine.state.epoch_length} batches")

    cache.run(dataset)
    dataset.cache_populated = True

def create_train_and_validation_engines(train_func, val_func=None, device='cpu'):
    """
    Helper function for creating an ignite Engine object with helpful defaults.
    This sets up an Engine that has four handlers attached to it:

    - prepare_batch: before a batch is passed to train_func or val_func, this
      function runs, moving every item in the batch (which is a dictionary) to
      the appropriate device ('cpu'  or 'cuda').

    - book_keeping: sets up some dictionaries that are used for bookkeeping so one
      can easily track the epoch and iteration losses for both training and
      validation.

    - add_to_iter_history: records the iteration, epoch, and past iteration losses
      into the dictionaries set up by book_keeping.

    - clear_iter_history: resets the current iteration history of losses after moving
      the current iteration history into past iteration history.
    
    Args:
        train_func (func): Function that provides the closure for training for
          a single batch.
        val_func (func, optional): Function that provides the closure for
          validating a single batch. Defaults to None.
        device (str, optional): Device to move tensors to. Defaults to 'cpu'.
    """
    # Set up engines for training and validation
    trainer = Engine(train_func)
    trainer.register_events(*ValidationEvents)
    trainer.register_events(*BackwardsEvents)
    
    validator = None if val_func is None else Engine(val_func)

    # Before a batch starts, the items should be float and moved to the 
    # correct device, for both training and validation. Checks to make
    # sure "cuda" is available if user requested cuda.
    device = torch.device(device)
    if not torch.cuda.is_available():
        device = torch.device('cpu')

    def prepare_batch(engine):
        batch = engine.state.batch
        for key in batch:
            if torch.is_tensor(batch[key]):
                batch[key] = batch[key].float().to(device)
        engine.state.batch = batch
    
    # Set up stuff for bookkeeping as training progresses.
    def book_keeping(engine):
        engine.state.epoch_history = {}
        engine.state.iter_history = {}
        engine.state.past_iter_history = {}

    def add_to_iter_history(engine):
        for key in engine.state.output:
            if key not in engine.state.iter_history:
                engine.state.iter_history[key] = []
            if key not in engine.state.past_iter_history:
                engine.state.past_iter_history[key] = []
            engine.state.iter_history[key].append(
                engine.state.output[key]
            )
            engine.state.past_iter_history[key].append(
                engine.state.iter_history[key]
            )
    
    def clear_iter_history(engine):
        engine.state.iter_history = {}

    trainer.add_event_handler(
        Events.ITERATION_STARTED, prepare_batch)
    trainer.add_event_handler(
        Events.STARTED, book_keeping)
    trainer.add_event_handler(
        Events.ITERATION_COMPLETED, add_to_iter_history)
    trainer.add_event_handler(
        Events.EPOCH_STARTED, clear_iter_history)

    if validator is not None:
        validator.add_event_handler(
            Events.ITERATION_STARTED, prepare_batch)
        validator.add_event_handler(
            Events.STARTED, book_keeping)
        validator.add_event_handler(
            Events.ITERATION_COMPLETED, add_to_iter_history)
        validator.add_event_handler(
            Events.EPOCH_STARTED, clear_iter_history)

    return trainer, validator


def add_validate_and_checkpoint(output_folder, model, optimizer, train_data, trainer, 
    val_data=None, validator=None):
    """
    This adds the following handler to the trainer:

    - validate_and_checkpoint: this runs the validator on the validation dataset 
      (``val_data``) using a defined validation process function ``val_func``. 
      These are optional. If these are not provided, then no validator is run
      and the model is simply checkpointed. The model is always saved to 
      ``{output_folder}/checkpoints/latest.model.pth``. If the model is also the 
      one with the lowest validation loss, then it is *also* saved to
      ``{output_folder}/checkpoints/best.model.pth. This is attached to
      ``Events.EPOCH_COMPLETED`` on the trainer. After completion, it fires a
      ``ValidationEvents.VALIDATION_COMPLETED`` event.

    Args:
        model (torch.nn.Module): Model that is being trained (typically a SeparationModel).
          optimizer (torch.optim.Optimizer): Optimizer being used to train.

        train_data (BaseDataset): dataset that is being used to train the model. This is to
          save additional metadata information alongside the model checkpoint such as the
          STFTParams, dataset folder, length, list of transforms, etc.

        trainer (ignite.Engine): Engine for trainer

        validator (ignite.Engine, optional): Engine for validation. 
          Defaults to None.

        val_data (torch.utils.data.Dataset, optional): The validation data. 
          Defaults to None.
    """ 
    # When the trainer finishes an epoch, it should validate and save 
    # the model.
    @trainer.on(Events.EPOCH_COMPLETED)
    def validate_and_checkpoint(trainer):
        trainer.fire_event(ValidationEvents.VALIDATION_STARTED)

        is_best = True
        if validator is not None:
            validator.run(val_data)

            for key in validator.state.iter_history:
                _key = f"validation/{key}"
                if _key not in trainer.state.epoch_history:
                    trainer.state.epoch_history[_key] = []
                trainer.state.epoch_history[_key].append(np.mean(
                    validator.state.iter_history[key]
                ))
                
            if 'validation/loss' in trainer.state.epoch_history:
                cur = trainer.state.epoch_history['validation/loss'][-1]
                is_best = cur == min(trainer.state.epoch_history['validation/loss'])

        for key in trainer.state.iter_history:
            _key = f"train/{key}"
            if _key not in trainer.state.epoch_history:
                trainer.state.epoch_history[_key] = []
            trainer.state.epoch_history[_key].append(np.mean(
                trainer.state.iter_history[key]
            ))

        output_paths = [os.path.join(
            output_folder, 'checkpoints', 'latest.model.pth')]
        if is_best:
            output_paths.append(os.path.join(
                output_folder, 'checkpoints', 'best.model.pth'
            ))

        metadata = {
            'stft_params': train_data.stft_params,
            'sample_rate': train_data.sample_rate,
            'num_channels': train_data.num_channels,
            'folder': train_data.folder,
            'transforms': train_data.transform,
            'trainer.state_dict': {
                'epoch': trainer.state.epoch,
                'epoch_length': trainer.state.epoch_length,
                'max_epochs': trainer.state.max_epochs,
                'output': trainer.state.output,
                'metrics': trainer.state.metrics,
                'seed': trainer.state.seed,
            },
            'trainer.state.epoch_history': trainer.state.epoch_history,
        }

        for _path in output_paths:
            os.makedirs(os.path.join(
                output_folder, 'checkpoints'), exist_ok=True)
            if isinstance(model, nn.DataParallel):
                _model = model.module
            else:
                _model = model
            _model.save(_path, {'metadata': metadata})
            torch.save(optimizer.state_dict(), 
                _path.replace('model.pth', 'optimizer.pth'))

        trainer.state.saved_model_path = output_paths[-1]
        trainer.state.output_folder = output_folder
        trainer.fire_event(ValidationEvents.VALIDATION_COMPLETED)


def add_stdout_handler(trainer, validator=None):
    """
    This adds the following handler to the trainer engine, and also sets up
    Timers:

    - log_epoch_to_stdout: This logs the results of a model after it has trained
      for a single epoch on both the training and validation set. The output typically
      looks like this:

      .. code-block:: none

            EPOCH SUMMARY
            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            - Epoch number: 0010 / 0010
            - Training loss:   0.583591
            - Validation loss: 0.137209
            - Epoch took: 00:00:03
            - Time since start: 00:00:32
            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            Saving to test.
            Output @ tests/local/trainer
    
    Args:
        trainer (ignite.Engine): Engine for trainer

        validator (ignite.Engine, optional): Engine for validation. 
          Defaults to None.
    """
    # Set up timers for overall time taken and each epoch
    overall_timer = Timer(average=False)
    overall_timer.attach(trainer, 
        start=Events.STARTED, pause=Events.COMPLETED)

    epoch_timer = Timer(average=False)
    epoch_timer.attach(
        trainer, start=Events.EPOCH_STARTED, 
        pause=ValidationEvents.VALIDATION_COMPLETED)

    @trainer.on(ValidationEvents.VALIDATION_COMPLETED)
    def log_epoch_to_stdout(trainer):
        epoch_time = epoch_timer.value()
        epoch_time = time.strftime(
            "%H:%M:%S", time.gmtime(epoch_time))
        overall_time = overall_timer.value()
        overall_time = time.strftime(
            "%H:%M:%S", time.gmtime(overall_time))

        epoch_number = trainer.state.epoch 
        total_epochs = trainer.state.max_epochs

        try:
            validation_loss = (
                f"{trainer.state.epoch_history['validation/loss'][-1]:04f}")
        except:
            validation_loss = 'N/A'

        train_loss = trainer.state.epoch_history['train/loss'][-1]
        saved_model_path = trainer.state.saved_model_path

        logging_str = (
            f"\n\n"
            f"EPOCH SUMMARY \n"
            f"~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ \n"
            f"- Epoch number: {epoch_number:04d} / {total_epochs:04d} \n"                 
            f"- Training loss:   {train_loss:04f} \n"
            f"- Validation loss: {validation_loss} \n"    
            f"- Epoch took: {epoch_time} \n"                
            f"- Time since start: {overall_time} \n"       
            f"~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ \n"
            f"Saving to {saved_model_path}. \n"
            f"Output @ {trainer.state.output_folder} \n"
        )

        logging.info(logging_str)

def add_tensorboard_handler(output_folder, engine):
    """
    Every key in engine.state.epoch_history[-1] is logged to TensorBoard.
    
    Args:
        output_folder (str): Where the tensorboard logs should go. The output
          logs will go into a subfolder 'tensorboard'.

        trainer (ignite.Engine): The engine to log.
    """
    @engine.on(ValidationEvents.VALIDATION_COMPLETED)
    def log_to_tensorboard(engine):
        writer = SummaryWriter(
            os.path.join(output_folder, 'tensorboard'))
        for key in engine.state.epoch_history:
            writer.add_scalar(
                key, engine.state.epoch_history[key][-1], engine.state.epoch)
