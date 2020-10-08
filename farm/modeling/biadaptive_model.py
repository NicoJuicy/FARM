import copy
import json
import logging
import os
from argparse import Namespace
from pathlib import Path

import multiprocessing
import numpy
import torch
from torch import nn

from farm.conversion.onnx_optimization.bert_model_optimization import main as optimize_onnx_model
from farm.data_handler.data_silo import DataSilo
from farm.modeling.language_model import LanguageModel
from farm.modeling.prediction_head import PredictionHead, TextSimilarityHead
from farm.modeling.tokenization import Tokenizer
from farm.utils import MLFlowLogger as MlLogger, stack

logger = logging.getLogger(__name__)


class BaseBiAdaptiveModel:
    """
    Base Class for implementing AdaptiveModel with frameworks like PyTorch and ONNX.
    """

    subclasses = {}

    def __init_subclass__(cls, **kwargs):
        """ This automatically keeps track of all available subclasses.
        Enables generic load() for all specific AdaptiveModel implementation.
        """
        super().__init_subclass__(**kwargs)
        cls.subclasses[cls.__name__] = cls

    def __init__(self, prediction_heads):
        self.prediction_heads = prediction_heads

    @classmethod
    def load(cls, **kwargs):
        """
        Load corresponding AdaptiveModel Class(AdaptiveModel/ONNXAdaptiveModel) based on the
        files in the load_dir.

        :param kwargs: arguments to pass for loading the model.
        :return: instance of a model
        """
        if (Path(kwargs["load_dir"]) / "model.onnx").is_file():
            model = cls.subclasses["ONNXBiAdaptiveModel"].load(**kwargs)
        else:
            model = cls.subclasses["BiAdaptiveModel"].load(**kwargs)
        return model

    def logits_to_preds(self, logits, **kwargs):
        """
        Get predictions from all prediction heads.

        :param logits: logits, can vary in shape and type, depending on task
        :type logits: object
        :param label_maps: Maps from label encoding to label string
        :param label_maps: dict
        :return: A list of all predictions from all prediction heads
        """
        all_preds = []
        # collect preds from all heads
        for head, logits_for_head in zip(self.prediction_heads, logits):
            preds = head.logits_to_preds(logits=logits_for_head, **kwargs)
            all_preds.append(preds)
        return all_preds

    def formatted_preds(self, logits, language_model1, language_model2, **kwargs):
        """
        Format predictions for inference.

        :param logits: model logits
        :type logits: torch.tensor
        :param kwargs: placeholder for passing generic parameters
        :type kwargs: object
        :return: predictions in the right format
        """
        n_heads = len(self.prediction_heads)

        if n_heads == 0:
            # just return LM output (e.g. useful for extracting embeddings at inference time)
            preds1_final = language_model1.formatted_preds(logits=logits, **kwargs)
            preds2_final = language_model2.formatted_preds(logits=logits, **kwargs)

        elif n_heads == 1:
            preds_final = []
            # This try catch is to deal with the fact that sometimes we collect preds before passing it to
            # formatted_preds (see Inferencer._get_predictions_and_aggregate()) and sometimes we don't
            # (see Inferencer._get_predictions())
            try:
                preds = kwargs["preds"]
                temp = [y[0] for y in preds]
                preds_flat = [item for sublist in temp for item in sublist]
                kwargs["preds"] = preds_flat
            except KeyError:
                kwargs["preds"] = None
            head = self.prediction_heads[0]
            logits_for_head = logits[0]
            preds = head.formatted_preds(logits=logits_for_head, **kwargs)
            # TODO This is very messy - we need better definition of what the output should look like
            if type(preds) == list:
                preds_final += preds
            elif type(preds) == dict and "predictions" in preds:
                preds_final.append(preds)

        # This case is triggered by Natural Questions
        else:
            preds_final = [list() for _ in range(n_heads)]
            preds = kwargs["preds"]
            preds_for_heads = stack(preds)
            logits_for_heads = [None] * n_heads

            samples = [s for b in kwargs["baskets"] for s in b.samples]
            kwargs["samples"] = samples

            del kwargs["preds"]

            for i, (head, preds_for_head, logits_for_head) in enumerate(zip(self.prediction_heads, preds_for_heads, logits_for_heads)):
                preds = head.formatted_preds(logits=logits_for_head, preds=preds_for_head, **kwargs)
                preds_final[i].append(preds)

            # Look for a merge() function amongst the heads and if a single one exists, apply it to preds_final
            merge_fn = pick_single_fn(self.prediction_heads, "merge_formatted_preds")
            if merge_fn:
                preds_final = merge_fn(preds_final)

        return preds_final

    def connect_heads_with_processor(self, tasks, require_labels=True):
        """
        Populates prediction head with information coming from tasks.

        :param tasks: A dictionary where the keys are the names of the tasks and the values are the details of the task (e.g. label_list, metric, tensor name)
        :param require_labels: If True, an error will be thrown when a task is not supplied with labels)
        :return:
        """

        for head in self.prediction_heads:
            head.label_tensor_name = tasks[head.task_name]["label_tensor_name"]
            label_list = tasks[head.task_name]["label_list"]
            if not label_list and require_labels:
                raise Exception(f"The task \'{head.task_name}\' is missing a valid set of labels")
            label_list = tasks[head.task_name]["label_list"]
            head.label_list = label_list
            num_labels = len(label_list)
            head.metric = tasks[head.task_name]["metric"]

    @classmethod
    def _get_prediction_head_files(cls, load_dir, strict=True):
        load_dir = Path(load_dir)
        files = os.listdir(load_dir)
        config_files = [
            load_dir / f
            for f in files
            if "config.json" in f and "prediction_head" in f
        ]
        # sort them to get correct order in case of multiple prediction heads
        config_files.sort()
        return config_files

def loss_per_head_sum(loss_per_head, global_step=None, batch=None):
    """
    Input: loss_per_head (list of tensors), global_step (int), batch (dict)
    Output: aggregated loss (tensor)
    """
    return sum(loss_per_head)

class BiAdaptiveModel(nn.Module, BaseBiAdaptiveModel):
    """ PyTorch implementation containing all the modelling needed for your NLP task. Combines a language
    model and a prediction head. Allows for gradient flow back to the language model component."""

    def __init__(
        self,
        language_model1,
        language_model2,
        prediction_heads,
        embeds_dropout_prob,
        device,
        lm1_output_types=["per_sequence"],
        lm2_output_types=["per_sequence"],
        loss_aggregation_fn=None,
    ):
        """
        :param language_model: Any model that turns token ids into vector representations
        :type language_model: LanguageModel
        :param prediction_heads: A list of models that take embeddings and return logits for a given task
        :type prediction_heads: list
        :param embeds_dropout_prob: The probability that a value in the embeddings returned by the
           language model will be zeroed.
        :param embeds_dropout_prob: float
        :param lm_output_types: How to extract the embeddings from the final layer of the language model. When set
                                to "per_token", one embedding will be extracted per input token. If set to
                                "per_sequence", a single embedding will be extracted to represent the full
                                input sequence. Can either be a single string, or a list of strings,
                                one for each prediction head.
        :type lm_output_types: list or str
        :param device: The device on which this model will operate. Either "cpu" or "cuda".
        :param loss_aggregation_fn: Function to aggregate the loss of multiple prediction heads.
                                    Input: loss_per_head (list of tensors), global_step (int), batch (dict)
                                    Output: aggregated loss (tensor)
                                    Default is a simple sum:
                                    `lambda loss_per_head, global_step=None, batch=None: sum(tensors)`
                                    However, you can pass more complex functions that depend on the
                                    current step (e.g. for round-robin style multitask learning) or the actual
                                    content of the batch (e.g. certain labels)
                                    Note: The loss at this stage is per sample, i.e one tensor of
                                    shape (batchsize) per prediction head.
        :type loss_aggregation_fn: function
        """

        super(BiAdaptiveModel, self).__init__()
        self.device = device
        self.language_model1 = language_model1.to(device)
        self.lm1_output_dims = language_model1.get_output_dims()
        self.language_model2 = language_model2.to(device)
        self.lm2_output_dims = language_model2.get_output_dims()
        self.prediction_heads = nn.ModuleList([ph.to(device) for ph in prediction_heads])

        self.dropout1 = nn.Dropout(embeds_dropout_prob)
        self.dropout2 = nn.Dropout(embeds_dropout_prob)
        self.lm1_output_types = (
            [lm1_output_types] if isinstance(lm1_output_types, str) else lm1_output_types
        )
        self.lm2_output_types = (
            [lm2_output_types] if isinstance(lm2_output_types, str) else lm2_output_types
        )
        self.log_params()
        # default loss aggregation function is a simple sum (without using any of the optional params)
        if not loss_aggregation_fn:
            loss_aggregation_fn = loss_per_head_sum
        self.loss_aggregation_fn = loss_aggregation_fn

    def save(self, save_dir):
        """
        Saves the language model. This will generate a config file
        and model weights for each.

        :param save_dir: path to save to
        :type save_dir: Path
        """
        os.makedirs(save_dir, exist_ok=True)
        if not os.path.exists(Path.joinpath(save_dir, Path("lm1"))):
            os.makedirs(Path.joinpath(save_dir, Path("lm1")))
        if not os.path.exists(Path.joinpath(save_dir, Path("lm2"))):
            os.makedirs(Path.joinpath(save_dir, Path("lm2")))
        self.language_model1.save(Path.joinpath(save_dir, Path("lm1")))
        self.language_model2.save(Path.joinpath(save_dir, Path("lm2")))
        for i, ph in enumerate(self.prediction_heads):
            logger.info("prediction_head saving")
            ph.save(save_dir, i)

    @classmethod
    def load(cls, load_dir, device, strict=True, lm1_name="lm1", lm2_name="lm2", processor=None):
        """
        Loads an AdaptiveModel from a directory. The directory must contain:

        * language_model.bin
        * language_model_config.json
        * prediction_head_X.bin  multiple PH possible
        * prediction_head_X_config.json
        * processor_config.json config for transforming input
        * vocab.txt vocab file for language model, turning text to Wordpiece Tokens

        :param load_dir: location where adaptive model is stored
        :type load_dir: Path
        :param device: to which device we want to sent the model, either cpu or cuda
        :type device: torch.device
        :param lm_name: the name to assign to the loaded language model
        :type lm_name: str
        :param strict: whether to strictly enforce that the keys loaded from saved model match the ones in
                       the PredictionHead (see torch.nn.module.load_state_dict()).
                       Set to `False` for backwards compatibility with PHs saved with older version of FARM.
        :type strict: bool
        :param processor: populates prediction head with information coming from tasks
        :type processor: Processor
        """
        # Language Model
        if lm1_name:
            language_model1 = LanguageModel.load(os.path.join(load_dir, lm1_name))
        else:
            language_model1 = LanguageModel.load(load_dir)
        if lm2_name:
            language_model2 = LanguageModel.load(os.path.join(load_dir, lm2_name))
        else:
            language_model2 = LanguageModel.load(load_dir)

        # Prediction heads
        ph_config_files = cls._get_prediction_head_files(load_dir)
        prediction_heads = []
        ph_output_type = []
        for config_file in ph_config_files:
            head = PredictionHead.load(config_file, strict=False, load_weights=False)
            prediction_heads.append(head)
            ph_output_type.append(head.ph_output_type)

        model = cls(language_model1, language_model2, prediction_heads, 0.1, device)
        if processor:
            model.connect_heads_with_processor(processor.tasks)

        return model

    def logits_to_loss_per_head(self, logits, **kwargs):
        """
        Collect losses from each prediction head.

        :param logits: logits, can vary in shape and type, depending on task.
        :type logits: object
        :return: The per sample per prediciton head loss whose first two dimensions have length n_pred_heads, batch_size
        """
        all_losses = []
        for head, logits_for_one_head in zip(self.prediction_heads, logits):
            # check if PredictionHead connected to Processor
            assert hasattr(head, "label_tensor_name"), \
                (f"Label_tensor_names are missing inside the {head.task_name} Prediction Head. Did you connect the model"
                " with the processor through either 'model.connect_heads_with_processor(processor.tasks)'"
                " or by passing the processor to the Adaptive Model?")
            all_losses.append(head.logits_to_loss(logits=logits_for_one_head, **kwargs))
        return all_losses

    def logits_to_loss(self, logits, global_step=None, **kwargs):
        """
        Get losses from all prediction heads & reduce to single loss *per sample*.

        :param logits: logits, can vary in shape and type, depending on task
        :type logits: object
        :param global_step: number of current training step
        :type global_step: int
        :param kwargs: placeholder for passing generic parameters.
                       Note: Contains the batch (as dict of tensors), when called from Trainer.train().
        :type kwargs: object
        :return loss: torch.tensor that is the per sample loss (len: batch_size)
        """
        all_losses = self.logits_to_loss_per_head(logits, **kwargs)
        # This aggregates the loss per sample across multiple prediction heads
        # Default is sum(), but you can configure any fn that takes [Tensor, Tensor ...] and returns [Tensor]
        loss = self.loss_aggregation_fn(all_losses, global_step=global_step, batch=kwargs)
        return loss

    def prepare_labels(self, **kwargs):
        """
        Label conversion to original label space, per prediction head.

        :param label_maps: dictionary for mapping ids to label strings
        :type label_maps: dict[int:str]
        :return: labels in the right format
        """
        all_labels = []
        # for head, label_map_one_head in zip(self.prediction_heads):
        #     labels = head.prepare_labels(label_map=label_map_one_head, **kwargs)
        #     all_labels.append(labels)
        for head in self.prediction_heads:
            labels = head.prepare_labels(**kwargs)
            all_labels.append(labels)
        return all_labels

    def forward(self, **kwargs):
        """
        Push data through the whole model and returns logits. The data will propagate through the language
        model and each of the attached prediction heads.

        :param kwargs: Holds all arguments that need to be passed to the language model and prediction head(s).
        :return: all logits as torch.tensor or multiple tensors.
        """

        # Run forward pass of language model
        pooled_output1, pooled_output2 = self.forward_lm(**kwargs)

        # Run forward pass of (multiple) prediction heads using the output from above
        all_logits = []
        if len(self.prediction_heads) > 0:
            for head, lm1_out, lm2_out in zip(self.prediction_heads, self.lm1_output_types, self.lm2_output_types):
                # Choose relevant vectors from LM as output and perform dropout
                if lm1_out == "per_sequence" or lm1_out == "per_sequence_continuous":
                    output1 = self.dropout1(pooled_output1)
                else:
                    raise ValueError(
                        "Unknown extraction strategy from DPR model: {}".format(lm1_out)
                    )

                if lm2_out == "per_sequence" or lm2_out == "per_sequence_continuous":
                    output2 = self.dropout2(pooled_output2)
                else:
                    raise ValueError(
                        "Unknown extraction strategy from DPR model: {}".format(lm2_out)
                    )

                # Do the actual forward pass of a single head
                all_logits.append(head(output1, output2))
        else:
            # just return LM output (e.g. useful for extracting embeddings at inference time)
            all_logits.append((pooled_output1, pooled_output2))

        return all_logits

    def forward_lm(self, **kwargs):
        """
        Forward pass for the DPR model

        :param kwargs:
        :return:
        """
        pooled_output1, hidden_states1 = self.language_model1(**kwargs)
        pooled_output2, hidden_states2 = self.language_model2(**kwargs)

        return pooled_output1, pooled_output2

    def log_params(self):
        """
        Logs paramteres to generic logger MlLogger
        """
        params = {
            "lm1_type": self.language_model1.__class__.__name__,
            "lm1_name": self.language_model1.name,
            "lm1_output_types": ",".join(self.lm1_output_types),
            "lm2_type": self.language_model2.__class__.__name__,
            "lm2_name": self.language_model2.name,
            "lm2_output_types": ",".join(self.lm2_output_types),
            "prediction_heads": ",".join(
                [head.__class__.__name__ for head in self.prediction_heads]
            ),
        }
        try:
            MlLogger.log_params(params)
        except Exception as e:
            logger.warning(f"ML logging didn't work: {e}")

    def verify_vocab_size(self, vocab_size1, vocab_size2):
        """ Verifies that the model fits to the tokenizer vocabulary.
        They could diverge in case of custom vocabulary added via tokenizer.add_tokens()"""

        model1_vocab_len = self.language_model1.model.resize_token_embeddings(new_num_tokens=None).num_embeddings

        msg = f"Vocab size of tokenizer {vocab_size1} doesn't match with model {model1_vocab_len}. " \
              "If you added a custom vocabulary to the tokenizer, " \
              "make sure to supply 'n_added_tokens' to LanguageModel.load() and BertStyleLM.load()"
        assert vocab_size1 == model1_vocab_len, msg

        model2_vocab_len = self.language_model2.model.resize_token_embeddings(new_num_tokens=None).num_embeddings

        msg = f"Vocab size of tokenizer {vocab_size1} doesn't match with model {model2_vocab_len}. " \
              "If you added a custom vocabulary to the tokenizer, " \
              "make sure to supply 'n_added_tokens' to LanguageModel.load() and BertStyleLM.load()"
        assert vocab_size2 == model2_vocab_len, msg

    def get_language(self):
        return self.language_model1.language, self.language_model2.language

    def convert_to_transformers(self):
        if len(self.prediction_heads) != 1:
            raise ValueError(f"Currently conversion only works for models with a SINGLE prediction head. "
                             f"Your model has {len(self.prediction_heads)}")
        elif len(self.prediction_heads[0].layer_dims) != 2:
            raise ValueError(f"Currently conversion only works for PredictionHeads that are a single layer Feed Forward NN with dimensions [LM_output_dim, number_classes].\n"
                             f"            Your PredictionHead has {str(self.prediction_heads[0].layer_dims)} dimensions.")
        #TODO add more infos to config
        if self.prediction_heads[0].model_type == "language_modelling":
            # init model
            transformers_model1 = AutoModelWithLMHead.from_config(self.language_model1.model.config)
            transformers_model2 = AutoModelWithLMHead.from_config(self.language_model2.model.config)
            # transfer weights for language model + prediction head
            setattr(transformers_model1, transformers_model1.base_model_prefix, self.language_model1.model)
            setattr(transformers_model2, transformers_model2.base_model_prefix, self.language_model2.model)
            logger.warning("No prediction head weights are required for DPR")

        else:
            raise NotImplementedError(f"FARM -> Transformers conversion is not supported yet for"
                                      f" prediction heads of type {self.prediction_heads[0].model_type}")
        pass

        return transformers_model1, transformers_model2

    @classmethod
    def convert_from_transformers(cls, model_name_or_path1, model_name_or_path2, device, task_type, processor=None):
        """
        Load a (downstream) model from huggingface's transformers format. Use cases:
         - continue training in FARM (e.g. take a squad QA model and fine-tune on your own data)
         - compare models without switching frameworks
         - use model directly for inference

        :param model_name_or_path: local path of a saved model or name of a public one.
                                              Exemplary public names:
                                              - distilbert-base-uncased-distilled-squad
                                              - deepset/bert-large-uncased-whole-word-masking-squad2

                                              See https://huggingface.co/models for full list
        :param device: "cpu" or "cuda"
        :param task_type: One of :
                          - 'question_answering'
                          - 'text_classification'
                          - 'embeddings'
                          More tasks coming soon ...
        :param processor: populates prediction head with information coming from tasks
        :type processor: Processor
        :return: AdaptiveModel
        """
        lm1 = LanguageModel.load(model_name_or_path1)
        lm2 = LanguageModel.load(model_name_or_path2)
        #TODO Infer type of head automatically from config
        if task_type == "text_similarity":
            bi_adaptive_model = cls(language_model1=lm1, language_model2=lm2, prediction_heads=[], embeds_dropout_prob=0.1,
                                 lm_output_types=["per_sequence"], device=device)
        else:
            raise NotImplementedError(f"Huggingface's transformer models of type {task_type} are not supported yet for BiAdaptive Models")

        if processor:
            bi_adaptive_model.connect_heads_with_processor(processor.tasks)

        return bi_adaptive_model

    '''
    def convert_to_onnx(self, output_path, opset_version=11, optimize_for=None):
        """
        Convert a PyTorch AdaptiveModel to ONNX.

        The conversion is trace-based by performing a forward pass on the model with a input batch.

        :param output_path: model dir to write the model and config files
        :type output_path: Path
        :param opset_version: ONNX opset version
        :type opset_version: int
        :param optimize_for: optimize the exported model for a target device. Available options
                             are "gpu_tensor_core" (GPUs with tensor core like V100 or T4),
                             "gpu_without_tensor_core" (most other GPUs), and "cpu".
        :type optimize_for: str
        :return:
        """
        if type(self.prediction_heads[0]) is not QuestionAnsweringHead:
            raise NotImplementedError

        tokenizer = Tokenizer.load(
            pretrained_model_name_or_path="deepset/bert-base-cased-squad2"
        )

        label_list = ["start_token", "end_token"]
        metric = "squad"
        max_seq_len = 384
        batch_size = 1
        processor = SquadProcessor(
            tokenizer=tokenizer,
            max_seq_len=max_seq_len,
            label_list=label_list,
            metric=metric,
            train_filename="stub-file",  # the data is loaded from dicts instead of file.
            dev_filename=None,
            test_filename=None,
            data_dir="stub-dir",
        )

        data_silo = DataSilo(processor=processor, batch_size=1, distributed=False, automatic_loading=False)
        sample_dict = [
            {
                "context": 'The Normans were the people who in the 10th and 11th centuries gave their name to Normandy, '
                           'a region in France. They were descended from Norse ("Norman" comes from "Norseman") raiders '
                           'and pirates from Denmark, Iceland and Norway who, under their leader Rollo, agreed to swear '
                           'fealty to King Charles III of West Francia.',
                "qas": [
                    {
                        "question": "In what country is Normandy located?",
                        "id": "56ddde6b9a695914005b9628",
                        "answers": [],
                        "is_impossible": False,
                    }
                ],
            }
        ]

        data_silo._load_data(train_dicts=sample_dict)
        data_loader = data_silo.get_data_loader("train")
        data = next(iter(data_loader))
        data = list(data.values())

        inputs = {
            'input_ids': data[0].to(self.device).reshape(batch_size, max_seq_len),
            'padding_mask': data[1].to(self.device).reshape(batch_size, max_seq_len),
            'segment_ids': data[2].to(self.device).reshape(batch_size, max_seq_len)
        }

        # The method argument passing in torch.onnx.export is different to AdaptiveModel's forward().
        # To resolve that, an ONNXWrapper instance is used.
        model = ONNXWrapper.load_from_adaptive_model(self)

        if not os.path.exists(output_path):
            os.makedirs(output_path)

        with torch.no_grad():
            symbolic_names = {0: 'batch_size', 1: 'max_seq_len'}
            torch.onnx.export(model,
                              args=tuple(inputs.values()),
                              f=output_path / 'model.onnx'.format(opset_version),
                              opset_version=opset_version,
                              do_constant_folding=True,
                              input_names=['input_ids',
                                           'padding_mask',
                                           'segment_ids'],
                              output_names=['logits'],
                              dynamic_axes={'input_ids': symbolic_names,
                                            'padding_mask': symbolic_names,
                                            'segment_ids': symbolic_names,
                                            'logits': symbolic_names,
                                            })

        if optimize_for:
            optimize_args = Namespace(
                disable_attention=False, disable_bias_gelu=False, disable_embed_layer_norm=False, opt_level=99,
                disable_skip_layer_norm=False, disable_bias_skip_layer_norm=False, hidden_size=768, verbose=False,
                input='onnx-export/model.onnx', model_type='bert', num_heads=12, output='onnx-export/model.onnx'
            )

            if optimize_for == "gpu_tensor_core":
                optimize_args.float16 = True
                optimize_args.input_int32 = True
            elif optimize_for == "gpu_without_tensor_core":
                optimize_args.float16 = False
                optimize_args.input_int32 = True
            elif optimize_for == "cpu":
                logger.info("")
                optimize_args.float16 = False
                optimize_args.input_int32 = False
            else:
                raise NotImplementedError(f"ONNXRuntime model optimization is not available for {optimize_for}. Choose "
                                          f"one of 'gpu_tensor_core'(V100 or T4), 'gpu_without_tensor_core' or 'cpu'.")

            optimize_onnx_model(optimize_args)
        else:
            logger.info("Exporting unoptimized ONNX model. To enable optimization, supply "
                        "'optimize_for' parameter with the target device.'")

        # PredictionHead contains functionalities like logits_to_preds() that would still be needed
        # for Inference with ONNX models. Only the config of the PredictionHead is stored.
        for i, ph in enumerate(self.prediction_heads):
            ph.save_config(output_path, i)

        processor.save(output_path)

        onnx_model_config = {
            "onnx_opset_version": opset_version,
            "language": self.get_language(),
        }
        with open(output_path / "model_config.json", "w") as f:
            json.dump(onnx_model_config, f)

        logger.info(f"Model exported at path {output_path}")
    '''

class ONNXBiAdaptiveModel(BaseBiAdaptiveModel):
    """
    Implementation of ONNX Runtime for Inference of ONNX Models.

    Existing PyTorch based FARM AdaptiveModel can be converted to ONNX format using AdaptiveModel.convert_to_onnx().
    The conversion is currently only implemented for Question Answering Models.

    For inference, this class is compatible with the FARM Inferencer.
    """
    def __init__(self, onnx_session, prediction_heads, language, device):
        if str(device) == "cuda" and onnxruntime.get_device() != "GPU":
            raise Exception(f"Device {device} not available for Inference. For CPU, run pip install onnxruntime and"
                            f"for GPU run pip install onnxruntime-gpu")
        self.onnx_session = onnx_session
        self.prediction_heads = prediction_heads
        self.device = device
        self.language = language

    @classmethod
    def load(cls, load_dir, device, **kwargs):
        import onnxruntime
        sess_options = onnxruntime.SessionOptions()
        # Set graph optimization level to ORT_ENABLE_EXTENDED to enable bert optimization.
        sess_options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
        # Use OpenMP optimizations. Only useful for CPU, has little impact for GPUs.
        sess_options.intra_op_num_threads = multiprocessing.cpu_count()
        onnx_session = onnxruntime.InferenceSession(str(load_dir / "model.onnx"), sess_options)

        # Prediction heads
        _, ph_config_files = cls._get_prediction_head_files(load_dir, strict=False)
        prediction_heads = []
        ph_output_type = []
        for config_file in ph_config_files:
            # ONNX Model doesn't need have a separate neural network for PredictionHead. It only uses the
            # instance methods of PredictionHead class, so, we load with the load_weights param as False.
            head = PredictionHead.load(config_file, load_weights=False)
            prediction_heads.append(head)
            ph_output_type.append(head.ph_output_type)

        with open(load_dir/"model_config.json") as f:
            model_config = json.load(f)
            language = model_config["language"]

        return cls(onnx_session, prediction_heads, language, device)

    def forward(self, **kwargs):
        """
        Perform forward pass on the model and return the logits.

        :param kwargs: all arguments that needs to be passed on to the model
        :return: all logits as torch.tensor or multiple tensors.
        """
        with torch.no_grad():
            input_to_onnx = {
                'input_ids': numpy.ascontiguousarray(kwargs['input_ids'].cpu().numpy()),
                'padding_mask': numpy.ascontiguousarray(kwargs['padding_mask'].cpu().numpy()),
                'segment_ids': numpy.ascontiguousarray(kwargs['segment_ids'].cpu().numpy()),
            }
            res = self.onnx_session.run(None, input_to_onnx)
            logits = [torch.from_numpy(res[0]).to(self.device)]

        return logits

    def eval(self):
        """
        Stub to make ONNXAdaptiveModel compatible with the PyTorch AdaptiveModel.
        """
        return True

    def get_language(self):
        """
        Get the language(s) the model was trained for.
        :return: str
        """
        return self.language


class ONNXWrapper(BiAdaptiveModel):
    """
    Wrapper Class for converting PyTorch models to ONNX.

    As of torch v1.4.0, torch.onnx.export only support passing positional arguments to the forward pass of the model.
    However, the AdaptiveModel's forward takes keyword arguments. This class circumvents the issue by converting
    positional arguments to keyword arguments.
    """
    @classmethod
    def load_from_adaptive_model(cls, adaptive_model):
        model = copy.deepcopy(adaptive_model)
        model.__class__ = ONNXWrapper
        return model

    def forward(self, *batch):
        return super().forward(input_ids=batch[0], padding_mask=batch[1], segment_ids=batch[2])
