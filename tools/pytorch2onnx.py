import argparse
import os.path as osp
from functools import partial

import mmcv
import numpy as np
import onnx
import onnxruntime as rt
import torch
from mmcv.runner import load_checkpoint
from onnx_util.symbolic import register_extra_symbolics

from mmdet.models import build_detector


def pytorch2onnx(model,
                 input_img,
                 input_shape,
                 opset_version=11,
                 show=False,
                 output_file='tmp.onnx',
                 verify=False):
    model.cpu().eval()
    # read image
    one_img = mmcv.imread(input_img, 'color')
    one_img = mmcv.imresize(one_img, input_shape[2:]).transpose(2, 0, 1)
    one_img = torch.from_numpy(one_img).unsqueeze(0).float()
    (_, C, H, W) = input_shape
    one_meta = {
        'img_shape': (H, W, C),
        'ori_shape': (H, W, C),
        'pad_shape': (H, W, C),
        'filename': '<demo>.png',
        'scale_factor': 1.0,
        'flip': False
    }
    # onnx.export does not support kwargs
    origin_forward = model.forward
    model.forward = partial(
        model.forward, img_metas=[[one_meta]], return_loss=False)
    # pytorch has some bug in pytorch1.3, we have to fix it
    # by replacing these existing op
    register_extra_symbolics(opset_version)
    torch.onnx.export(
        model, ([one_img]),
        output_file,
        export_params=True,
        keep_initializers_as_inputs=True,
        verbose=show,
        opset_version=opset_version)
    model.forward = origin_forward
    print(f'Successfully exported ONNX model: {output_file}')
    if verify:
        # check by onnx
        onnx_model = onnx.load(output_file)
        onnx.checker.check_model(onnx_model)

        # check the numerical value
        # get pytorch output
        pytorch_result = model([one_img], [[one_meta]], return_loss=False)

        # get onnx output
        input_all = [node.name for node in onnx_model.graph.input]
        input_initializer = [
            node.name for node in onnx_model.graph.initializer
        ]
        net_feed_input = list(set(input_all) - set(input_initializer))
        assert (len(net_feed_input) == 1)
        sess = rt.InferenceSession(output_file)
        from mmdet.core import bbox2result
        det_bboxes, det_labels = sess.run(
            None, {net_feed_input[0]: one_img.detach().numpy()})
        # only compare a part of result
        bbox_results = bbox2result(det_bboxes, det_labels, 1)
        onnx_results = bbox_results[0]
        assert (np.abs(
            (pytorch_result[0][:, 4] - onnx_results[:, 4])) > 0.01).sum(
            ) == 0, 'The outputs are different between Pytorch and ONNX'
        print('The numerical values are same between Pytorch and ONNX')


def parse_args():
    parser = argparse.ArgumentParser(description='Convert MMDet to ONNX')
    parser.add_argument('config', help='test config file path')
    parser.add_argument('checkpoint', help='checkpoint file')
    parser.add_argument('--input_img', type=str, help='Images for input')
    parser.add_argument('--show', action='store_true', help='show onnx graph')
    parser.add_argument('--output_file', type=str, default='tmp.onnx')
    parser.add_argument('--opset_version', type=int, default=11)
    parser.add_argument(
        '--verify',
        action='store_true',
        help='verify the onnx model output against pytorch output')
    parser.add_argument(
        '--shape',
        type=int,
        nargs='+',
        default=[800, 1216],
        help='input image size')
    args = parser.parse_args()
    return args


if __name__ == '__main__':
    args = parse_args()

    assert args.opset_version == 11, 'MMDet only support opset 11 now'

    if not args.input_img:
        args.input_img = osp.join(
            osp.dirname(__file__), '../tests/data/color.jpg')

    if len(args.shape) == 1:
        input_shape = (1, 3, args.shape[0], args.shape[0])
    elif len(args.shape) == 2:
        input_shape = (
            1,
            3,
        ) + tuple(args.shape)
    else:
        raise ValueError('invalid input shape')

    cfg = mmcv.Config.fromfile(args.config)
    cfg.model.pretrained = None
    cfg.data.test.test_mode = True

    # build the model
    model = build_detector(cfg.model, train_cfg=None, test_cfg=cfg.test_cfg)
    checkpoint = load_checkpoint(model, args.checkpoint, map_location='cpu')
    # old versions did not save class info in checkpoints,
    # this walkaround is for backward compatibility
    if 'CLASSES' in checkpoint['meta']:
        model.CLASSES = checkpoint['meta']['CLASSES']

    # conver model to onnx file
    pytorch2onnx(
        model,
        args.input_img,
        input_shape,
        opset_version=args.opset_version,
        show=args.show,
        output_file=args.output_file,
        verify=args.verify)
