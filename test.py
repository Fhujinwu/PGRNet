import shutil
import SimpleITK as sitk
import torch.nn as nn
import torch.optim
from torch.utils.data import DataLoader
from models.fewshot import resnet101
from dataloaders.datasets import TestDataset
from dataloaders.dataset_specifics import *
from utils import *
from config import ex
from util.vis import Visualizer
import matplotlib.pyplot as plt
import torch.backends.cudnn as cudnndata_config
import torch.backends.cudnn as cudnn
import cv2
import warnings
warnings.filterwarnings("ignore")

@ex.automain
def main(_run, _config, _log):
    if _run.observers:
        os.makedirs(f'{_run.observers[0].dir}/interm_preds', exist_ok=True)
        for source_file, _ in _run.experiment_info['sources']:
            os.makedirs(os.path.dirname(f'{_run.observers[0].dir}/source/{source_file}'),
                        exist_ok=True)
            _run.observers[0].save_file(source_file, f'source/{source_file}')
        shutil.rmtree(f'{_run.observers[0].basedir}/_sources')

        # Set up logger -> log to .txt
        file_handler = logging.FileHandler(os.path.join(f'{_run.observers[0].dir}', f'logger.log'))
        file_handler.setLevel('INFO')
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
        file_handler.setFormatter(formatter)
        _log.handlers.append(file_handler)
        _log.info(f'Run "{_config["exp_str"]}" with ID "{_run.observers[0].dir[-1]}"')

    # Deterministic setting for reproduciablity.
    if _config['seed'] is not None:
        random.seed(_config['seed'])
        torch.manual_seed(_config['seed'])
        torch.cuda.manual_seed_all(_config['seed'])
        cudnn.deterministic = True

    # Enable cuDNN benchmark mode to select the fastest convolution algorithm.
    cudnn.enabled = True
    cudnn.benchmark = True
    torch.cuda.set_device(device=_config['gpu_id'])
    torch.set_num_threads(1)

    _log.info(f'Create model...')
    model = resnet101()
    model.cuda()
    model = nn.DataParallel(model)
    model.load_state_dict(torch.load(_config['reload_model_path'], map_location='cpu'))

    Visualizer.initialize(_config['visualize'], vis_path=_config['vis_path'])

    _log.info(f'Load data...')
    data_config = {
        'data_dir': _config['path'][_config['dataset']]['data_dir'],
        'dataset': _config['dataset'],
        'n_shot': _config['n_shot'],
        'n_way': _config['n_way'],
        'n_query': _config['n_query'],
        'n_sv': _config['n_sv'],
        'max_iter': _config['max_iters_per_load'],
        'eval_fold': _config['eval_fold'],
        'min_size': _config['min_size'],
        'max_slices': _config['max_slices'],
        'supp_idx': _config['supp_idx'],
    }
    test_dataset = TestDataset(data_config)
    test_loader = DataLoader(test_dataset,
                             batch_size=_config['batch_size'],
                             shuffle=False,
                             num_workers=_config['num_workers'],
                             pin_memory=True,
                             drop_last=True)

    # Get unique labels (classes).
    labels = get_label_names(_config['dataset'])

    # Loop over classes.
    class_dice = {}
    class_iou = {}
    counter = 0

    _log.info(f'Starting validation...')
    for label_val, label_name in labels.items():

        # Skip BG class.
        if label_name == 'BG':
            continue
        elif (not np.intersect1d([label_val], _config['test_label'])):
            continue

        _log.info(f'Test Class: {label_name}')

        # Get support sample + mask for current class.
        support_sample = test_dataset.getSupport(label=label_val, all_slices=False, N=_config['n_part'])
        test_dataset.label = label_val

        # Test.
        with torch.no_grad():
            model.eval()

            # Unpack support data.
            support_image = [support_sample['image'][[i]].float().cuda() for i in
                             range(support_sample['image'].shape[0])]  # n_shot x 3 x H x W
            support_fg_mask = [support_sample['label'][[i]].float().cuda() for i in
                               range(support_sample['image'].shape[0])]  # n_shot x H x W

            # Loop through query volumes.
            scores = Scores()
            single_score = Scores()
            for i, sample in enumerate(test_loader):

                # Unpack query data.
                query_image = [sample['image'][i].float().cuda() for i in
                               range(sample['image'].shape[0])]  # [C x 3 x H x W]
                query_label = sample['label'].long()  # C x H x W
                query_id = sample['id'][0].split('image_')[1][:-len('.nii.gz')]

                # Compute output.
                # Match support slice and query sub-chunck.
                query_pred = torch.zeros(query_label.shape[-3:])
                C_q = sample['image'].shape[1]
                idx_ = np.linspace(0, C_q, _config['n_part'] + 1).astype('int')
                for sub_chunck in range(_config['n_part']):
                    support_image_s = torch.cat([torch.cat([support_image[sub_chunck]], dim=0)], dim=0)  # 1 x 3 x H x W
                    support_fg_mask_s = torch.cat([torch.cat([support_fg_mask[sub_chunck]], dim=0)], dim=0)   # 1 x H x W
                    query_image_s = torch.cat([query_image[0][idx_[sub_chunck]:idx_[sub_chunck + 1]]], dim=0)  # C' x 3 x H x W
                    query_pred_s = []

                    for i in range(query_image_s.shape[0]):

                        _pred_s = model(query_image_s[[i]], support_image_s, support_fg_mask_s.unsqueeze(dim=1)
                                           )  # C x 2 x H x W

                        _pred_s = nn.functional.interpolate(_pred_s, size=query_label.shape[-2:],
                                                               mode='bilinear', align_corners=True)

                        if Visualizer.visualize:
                            single_score.record(_pred_s.squeeze().argmax(dim=0).cpu(), query_label[:, [i], :, :])
                            Visualizer.visualize_prediction_batch(torch.stack(support_image_s, dim=0),
                                                                  torch.stack(support_fg_mask_s, dim=0),
                                                                  query_image_s[[i]], query_label.squeeze()[[i]],
                                                                  _pred_s.argmax(dim=1).cpu(),
                                                                  [label_name] * (query_label.squeeze()[[i]].shape[0]), i,
                                                                  torch.zeros(_pred_s.shape[0]).fill_(
                                                                      single_score.patient_iou[-1].item()))

                        query_pred_s.append(_pred_s)
                    query_pred_s = torch.cat(query_pred_s, dim=0)
                    query_pred_s = query_pred_s.argmax(dim=1).cpu()  # C x H x W
                    query_pred[idx_[sub_chunck]:idx_[sub_chunck + 1]] = query_pred_s

                # Record scores.
                scores.record(query_pred, query_label)

                # Log.
                _log.info(
                    f'Tested query volume: {sample["id"][0][len(_config["path"][_config["dataset"]]["data_dir"]):]}.')
                _log.info(f'Dice score: {scores.patient_dice[-1].item()}')

                # Save predictions.
                file_name = os.path.join(f'{_run.observers[0].dir}/interm_preds',
                                         f'prediction_{query_id}_{label_name}.nii.gz')
                itk_pred = sitk.GetImageFromArray(query_pred)
                sitk.WriteImage(itk_pred, file_name, True)
                _log.info(f'{query_id} has been saved. ')

            # Log class-wise results
            class_dice[label_name] = torch.tensor(scores.patient_dice).mean().item()
            class_iou[label_name] = torch.tensor(scores.patient_iou).mean().item()
            _log.info(f'Test Class: {label_name}')
            _log.info(f'Mean class IoU: {class_iou[label_name]}')
            _log.info(f'Mean class Dice: {class_dice[label_name]}')

    _log.info(f'Final results...')
    _log.info(f'Mean IoU: {class_iou}')
    _log.info(f'Mean Dice: {class_dice}')

    _log.info(f'End of validation.')
    return 1
