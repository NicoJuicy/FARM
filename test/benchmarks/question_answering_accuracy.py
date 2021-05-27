import logging
import os
from pathlib import Path
from time import time

import numpy as np
from pprint import pformat
import pandas as pd
from dotmap import DotMap

from farm.data_handler.data_silo import DataSilo
from farm.data_handler.processor import SquadProcessor
from farm.data_handler.utils import write_squad_predictions
from farm.eval import Evaluator
from farm.evaluation import squad_evaluation
from farm.infer import Inferencer
from farm.modeling.adaptive_model import AdaptiveModel
from farm.modeling.language_model import LanguageModel
from farm.modeling.optimization import initialize_optimizer, optimize_model
from farm.modeling.prediction_head import QuestionAnsweringHead
from farm.modeling.tokenization import Tokenizer
from farm.train import Trainer
from farm.utils import set_all_seeds, initialize_device_settings

logger = logging.getLogger(__name__)
n_gpu_factor=4
error_messages = []

def test_evaluation():
    ##########################
    ########## Settings
    ##########################
    lang_model = "deepset/roberta-base-squad2"
    do_lower_case = False

    test_assertions = False

    data_dir = Path("testsave/data/squad20")
    evaluation_filename = "dev-v2.0.json"

    device, n_gpu = initialize_device_settings(use_cuda=True)

    # loading models and evals
    model = AdaptiveModel.convert_from_transformers(lang_model, device=device, task_type="question_answering")
    model.prediction_heads[0].no_ans_boost = 0
    model.prediction_heads[0].n_best = 1

    tokenizer = Tokenizer.load(pretrained_model_name_or_path=lang_model,do_lower_case=do_lower_case)
    processor = SquadProcessor(
        tokenizer=tokenizer,
        max_seq_len=256,
        label_list= ["start_token", "end_token"],
        metric="squad",
        train_filename=None,
        dev_filename=None,
        dev_split=0,
        test_filename=evaluation_filename,
        data_dir=data_dir,
        doc_stride=128,
    )

    starttime = time()

    data_silo = DataSilo(processor=processor, batch_size=40*n_gpu_factor, max_processes=1)
    model.connect_heads_with_processor(data_silo.processor.tasks, require_labels=True)
    model, _ = optimize_model(model=model, device=device, local_rank=-1, optimizer=None, distributed=False, use_amp=None)

    evaluator = Evaluator(data_loader=data_silo.get_data_loader("test"), tasks=data_silo.processor.tasks, device=device)

    # 1. Test FARM internal evaluation
    results = evaluator.eval(model)
    f1_score = results[0]["f1"] * 100
    em_score = results[0]["EM"] * 100
    tnacc = results[0]["top_n_accuracy"] * 100
    elapsed = time() - starttime
    print(results)
    print(elapsed)

    gold_EM = 78.4721
    gold_f1 = 82.6671
    gold_tnacc = 84.3594 # top 1 recall
    gold_elapsed = 40 # 4x V100
    if test_assertions:
        np.testing.assert_allclose(em_score, gold_EM, rtol=0.001, err_msg=f"FARM Eval changed for EM by: {em_score-gold_EM}")
        np.testing.assert_allclose(f1_score, gold_f1, rtol=0.001, err_msg=f"FARM Eval changed for f1 score by: {f1_score-gold_f1}")
        np.testing.assert_allclose(tnacc, gold_tnacc, rtol=0.001, err_msg=f"FARM Eval changed for top 1 accuracy by: {tnacc-gold_tnacc}")
        np.testing.assert_allclose(elapsed, gold_elapsed, rtol=0.1, err_msg=f"FARM Eval speed changed significantly by: {elapsed - gold_elapsed} seconds")

    if not np.allclose(f1_score, gold_f1, rtol=0.001):
        error_messages.append(f"FARM Eval changed for f1 score by: {round(f1_score - gold_f1, 4)}")
    if not np.allclose(em_score, gold_EM, rtol=0.001):
        error_messages.append(f"FARM Eval changed for EM by: {round(em_score - gold_EM, 4)}")
    if not np.allclose(tnacc, gold_tnacc, rtol=0.001):
        error_messages.append(f"FARM Eval changed for top 1 accuracy by: {round(tnacc-gold_tnacc, 4)}")
    if not np.allclose(elapsed, gold_elapsed, rtol=0.1):
        error_messages.append(f"FARM Eval speed changed significantly by: {round(elapsed - gold_elapsed, 4)} seconds")

    benchmark_result = [{ "run": "FARM internal evaluation",
          "f1_change": round(f1_score - gold_f1, 4),
          "em_change": round(em_score - gold_EM, 4),
          "tnacc_change": round(tnacc - gold_tnacc, 4),
          "elapsed_change": round(elapsed - gold_elapsed, 4),
          "f1": f1_score,
          "em": em_score,
          "tnacc": round(tnacc, 4),
          "elapsed": elapsed,
          "f1_gold": gold_f1,
          "em_gold": gold_EM,
          "tnacc_gold": gold_tnacc,
          "elapsed_gold": gold_elapsed
          }]
    logger.info("\n\n" + pformat(benchmark_result[0]) + "\n")

    # # 2. Test FARM predictions with outside eval script
    starttime = time()
    model = Inferencer(model=model, processor=processor, task_type="question_answering", batch_size=40*n_gpu_factor, gpu=device.type=="cuda", num_processes =1)
    filename = data_dir / evaluation_filename
    result = model.inference_from_file(file=filename, return_json=False, multiprocessing_chunksize=80)
    results_squad = [x.to_squad_eval() for x in result]
    model.close_multiprocessing_pool()

    elapsed = time() - starttime

    os.makedirs("../testsave", exist_ok=True)
    write_squad_predictions(
        predictions=results_squad,
        predictions_filename=filename,
        out_filename="testsave/predictions.json"
    )
    script_params = {"data_file": filename,
              "pred_file": "testsave/predictions.json",
              "na_prob_thresh" : 1,
              "na_prob_file": False,
              "out_file": False}
    results_official = squad_evaluation.main(OPTS=DotMap(script_params))
    f1_score = results_official["f1"]
    em_score = results_official["exact"]



    gold_EM = 79.878
    gold_f1 = 82.917
    gold_elapsed = 27 # 4x V100
    print(elapsed)
    if test_assertions:
        np.testing.assert_allclose(em_score, gold_EM, rtol=0.001,
                                   err_msg=f"Eval with official script changed for EM by: {em_score - gold_EM}")
        np.testing.assert_allclose(f1_score, gold_f1, rtol=0.001,
                                   err_msg=f"Eval with official script changed for f1 score by: {f1_score - gold_f1}")
        np.testing.assert_allclose(elapsed, gold_elapsed, rtol=0.1,
                                   err_msg=f"Inference speed changed significantly by: {elapsed - gold_elapsed} seconds")
    if not np.allclose(f1_score, gold_f1, rtol=0.001):
        error_messages.append(f"Eval with official script changed for f1 score by: {round(f1_score - gold_f1, 4)}")
    if not np.allclose(em_score, gold_EM, rtol=0.001):
        error_messages.append(f"Eval with official script changed for EM by: {round(em_score - gold_EM, 4)}")
    if not np.allclose(elapsed, gold_elapsed, rtol=0.1):
        error_messages.append(f"Inference speed changed significantly by: {round(elapsed - gold_elapsed,4)} seconds")

    benchmark_result.append({"run": "outside eval script",
          "f1_change": round(f1_score - gold_f1, 4),
          "em_change": round(em_score - gold_EM, 4),
          "tnacc_change": "-",
          "elapsed_change": round(elapsed - gold_elapsed, 4),
          "f1": f1_score,
          "em": em_score,
          "tnacc": "-",
          "elapsed": elapsed,
          "f1_gold": gold_f1,
          "em_gold": gold_EM,
          "tnacc_gold": "-",
          "elapsed_gold": gold_elapsed
          })
    logger.info("\n\n" + pformat(benchmark_result[1]) + "\n")
    return benchmark_result


def train_evaluation_single(seed=42):
    ##########################
    ########## Settings
    ##########################
    set_all_seeds(seed=seed)
    device, n_gpu = initialize_device_settings(use_cuda=True)
    # GPU utilization on 4x V100
    # 40*4, 14.3/16GB on master, 12.6/16 on others
    batch_size = 40*n_gpu_factor
    n_epochs = 2
    evaluate_every = 2000000 # disabling dev eval
    lang_model = "roberta-base"
    do_lower_case = False  # roberta is a cased model
    test_assertions = False
    train_filename = "train-v2.0.json"
    dev_filename = "dev-v2.0.json"


    # Load model and train
    tokenizer = Tokenizer.load(
        pretrained_model_name_or_path=lang_model, do_lower_case=do_lower_case
    )
    processor = SquadProcessor(
        tokenizer=tokenizer,
        max_seq_len=256,
        label_list=["start_token", "end_token"],
        metric="squad",
        train_filename=train_filename,
        dev_filename=dev_filename,
        test_filename=None,
        data_dir=Path("testsave/data/squad20"),
    )
    data_silo = DataSilo(processor=processor, batch_size=batch_size, max_processes=1)
    language_model = LanguageModel.load(lang_model)
    prediction_head = QuestionAnsweringHead(n_best=5)
    model = AdaptiveModel(
        language_model=language_model,
        prediction_heads=[prediction_head],
        embeds_dropout_prob=0.1,
        lm_output_types=["per_token"],
        device=device,
    )
    model, optimizer, lr_schedule = initialize_optimizer(
        model=model,
        learning_rate=3e-5,
        schedule_opts={"name": "LinearWarmup", "warmup_proportion": 0.2},
        n_batches=len(data_silo.loaders["train"]),
        n_epochs=n_epochs,
        device=device
    )
    trainer = Trainer(
        model=model,
        optimizer=optimizer,
        data_silo=data_silo,
        epochs=n_epochs,
        n_gpu=n_gpu,
        lr_schedule=lr_schedule,
        evaluate_every=evaluate_every,
        device=device,
    )
    starttime = time()
    trainer.train()
    elapsed = time() - starttime

    save_dir = Path("testsave/roberta-qa-dev")
    model.save(save_dir)
    processor.save(save_dir)

    # Create Evaluator
    evaluator = Evaluator(data_loader=data_silo.get_data_loader("dev"), tasks=data_silo.processor.tasks, device=device)

    results = evaluator.eval(model)
    f1_score = results[0]["f1"] * 100
    em_score = results[0]["EM"] * 100
    tnacc = results[0]["top_n_accuracy"] * 100

    print(results)
    print(elapsed)


    gold_f1 = 82.155
    gold_EM = 77.714
    gold_tnrecall = 97.3721 #
    gold_elapsed = 1135
    if test_assertions:
        np.testing.assert_allclose(f1_score, gold_f1, rtol=0.01,
                                   err_msg=f"FARM Training changed for f1 score by: {f1_score - gold_f1}")
        np.testing.assert_allclose(em_score, gold_EM, rtol=0.01,
                                   err_msg=f"FARM Training changed for EM by: {em_score - gold_EM}")
        np.testing.assert_allclose(tnacc, gold_tnrecall, rtol=0.01,
                                   err_msg=f"FARM Training changed for top 5 accuracy by: {tnacc - gold_tnrecall}")
        np.testing.assert_allclose(elapsed, gold_elapsed, rtol=0.1, err_msg=f"FARM Training speed changed significantly by: {elapsed - gold_elapsed} seconds")
    if not np.allclose(f1_score, gold_f1, rtol=0.01):
        error_messages.append(f"FARM Training changed for f1 score by: {round(f1_score - gold_f1, 4)}")
    if not np.allclose(em_score, gold_EM, rtol=0.01):
        error_messages.append(f"FARM Training changed for EM by: {round(em_score - gold_EM, 4)}")
    if not np.allclose(tnacc, gold_tnrecall, rtol=0.01):
        error_messages.append(f"FARM Training changed for top 5 accuracy by: {round(tnacc - gold_tnrecall, 4)}")
    if not np.allclose(elapsed, gold_elapsed, rtol=0.1):
        error_messages.append(f"FARM Training speed changed significantly by: {round(elapsed - gold_elapsed, 4)} seconds")

    benchmark_result = [{"run": "train evaluation",
              "f1_change": round(f1_score - gold_f1, 4),
              "em_change": round(em_score - gold_EM, 4),
              "tnacc_change": round(tnacc - gold_tnrecall, 4),
              "elapsed_change": round(elapsed - gold_elapsed, 4),
              "f1": f1_score,
              "em": em_score,
              "tnacc": round(tnacc, 4),
              "elapsed": elapsed,
              "f1_gold": gold_f1,
              "em_gold": gold_EM,
              "tnacc_gold": gold_tnrecall,
              "elapsed_gold": gold_elapsed
              }]
    logger.info("\n\n" + pformat(benchmark_result) + "\n")
    return benchmark_result

if __name__ == "__main__":
    logger.info("QA Accuracy Benchmark")
    benchmark_results = []
    benchmark_results.extend(test_evaluation())
    benchmark_results.extend(train_evaluation_single(seed=42))

    output_file = f"results_accuracy.csv"
    df = pd.DataFrame.from_records(benchmark_results)
    df.to_csv(output_file)
    with open(output_file.replace(".csv", ".md"), "w") as f:
        if error_messages:
            f.write("### :warning: QA Accuracy Benchmark Failed\n")
            for error_message in error_messages:
                f.write(error_message+"\n")
        else:
            f.write("### :heavy_check_mark: QA Accuracy Benchmark Passed\n")
        f.write(str(df.to_markdown()))

