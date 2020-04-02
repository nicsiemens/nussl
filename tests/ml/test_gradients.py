from nussl import ml, datasets, utils
import numpy as np
import torch
import matplotlib.pyplot as plt


def test_gradients(mix_source_folder):
    tfms = datasets.transforms.Compose([
        datasets.transforms.PhaseSensitiveSpectrumApproximation(),
        datasets.transforms.MagnitudeWeights(),
        datasets.transforms.ToSeparationModel(),
        datasets.transforms.GetExcerpt(400)
    ])
    dataset = datasets.MixSourceFolder(
        mix_source_folder, transform=tfms)

    # create the model, based on the first item in the dataset
    # second bit of the shape is the number of features
    n_features = dataset[0]['mix_magnitude'].shape[1]

    # make some configs
    names = ['dpcl', 'mask_inference_l1', 'mask_inference_mse_loss', 'chimera',
             'open_unmix']
    config_has_batch_norm = ['open_unmix']
    configs = [
        ml.networks.builders.build_recurrent_dpcl(
            n_features, 50, 1, True, 0.0, 20, ['sigmoid'],
            normalization_class='InstanceNorm'),
        ml.networks.builders.build_recurrent_mask_inference(
            n_features, 50, 1, True, 0.0, 2, ['softmax'],
            normalization_class='InstanceNorm'
        ),
        ml.networks.builders.build_recurrent_mask_inference(
            n_features, 50, 1, True, 0.0, 2, ['softmax'],
            normalization_class='InstanceNorm'
        ),
        ml.networks.builders.build_recurrent_chimera(
            n_features, 50, 1, True, 0.0, 20, ['sigmoid'], 2,
            ['softmax'], normalization_class='InstanceNorm'
        ),
        ml.networks.builders.build_open_unmix_like(
            n_features, 50, 1, True, .4, 2, 1, add_embedding=True,
            embedding_size=20, embedding_activation=['sigmoid', 'unit_norm'],
        )
    ]

    loss_dictionaries = [
        {
            'DeepClusteringLoss': {
                'weight': 1.0
            }
        },
        {
            'L1Loss': {
                'weight': 1.0
            }
        },
        {
            'MSELoss': {
                'weight': 1.0
            }
        },
        {
            'DeepClusteringLoss': {
                'weight': 0.2
            },
            'PermutationInvariantLoss': {
                'args': ['L1Loss'],
                'weight': 0.8
            }
        },
    ]

    for name, config, loss_dictionary in zip(names, configs, loss_dictionaries):
        loss_closure = ml.train.closures.Closure(loss_dictionary)

        utils.seed(0, set_cudnn=True)
        model_grad = ml.SeparationModel(config)
        model_grad.output_keys.append('normalization')

        all_data = {}
        for data in dataset:
            for key in data:
                if torch.is_tensor(data[key]):
                    data[key] = data[key].float().unsqueeze(0).contiguous()
                    if key not in all_data:
                        all_data[key] = data[key]
                    else:
                        all_data[key] = torch.cat([all_data[key], data[key]], dim=0)

        # do a forward pass in batched mode
        output_grad = model_grad(all_data)
        _loss = loss_closure.compute_loss(output_grad, all_data)
        # do a backward pass in batched mode
        _loss['loss'].backward()

        plt.figure(figsize=(10, 10))
        utils.visualize_gradient_flow(model_grad.named_parameters())
        plt.tight_layout()
        plt.savefig(f'tests/local/{name}:batch_gradient.png')

        utils.seed(0, set_cudnn=True)
        model_acc = ml.SeparationModel(config)
        model_acc.output_keys.append('normalization')

        for i, data in enumerate(dataset):
            for key in data:
                if torch.is_tensor(data[key]):
                    data[key] = data[key].float().unsqueeze(0).contiguous()
            # do a forward pass on each item individually
            output_acc = model_acc(data)
            for key in output_acc:
                # make sure the forward pass in batch and forward pass individually match
                # if they don't, then items in a minibatch are talking to each other 
                # somehow...
                _data_a = output_acc[key]
                _data_b = output_grad[key][i].unsqueeze(0)
                assert torch.allclose(_data_a, _data_b, atol=1e-4)

            _loss = loss_closure.compute_loss(output_acc, data)
            # do a backward pass on each item individually
            _loss['loss'] = _loss['loss'] / len(dataset)
            _loss['loss'].backward()

        plt.figure(figsize=(10, 10))
        utils.visualize_gradient_flow(model_acc.named_parameters())
        plt.tight_layout()
        plt.savefig(f'tests/local/{name}:accumulated_gradient.png')

        # make sure the gradients match between batched and accumulated gradients
        # if they don't, then the items in a batch are talking to each other in the loss
        for param1, param2 in zip(model_grad.parameters(), model_acc.parameters()):
            assert torch.allclose(param1, param2)
            if name not in config_has_batch_norm:
                assert torch.allclose(param1.grad, param2.grad, atol=1e-3)
