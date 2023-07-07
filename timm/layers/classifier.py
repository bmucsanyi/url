""" Classifier head and layer factory

Hacked together by / Copyright 2020 Ross Wightman
"""
from collections import OrderedDict
from functools import partial
from typing import Optional, Union, Callable

import torch
import torch.nn as nn
from torch.nn import functional as F

from .adaptive_avgmax_pool import SelectAdaptivePool2d
from .create_act import get_act_layer
from .create_norm import get_norm_layer
from .sngp_layers import RandomFeatureGaussianProcess


def _create_pool(num_features, num_classes, pool_type="avg", use_conv=False):
    flatten_in_pool = not use_conv  # flatten when we use a Linear layer after pooling
    if not pool_type:
        assert (
            num_classes == 0 or use_conv
        ), "Pooling can only be disabled if classifier is also removed or conv classifier is used"
        flatten_in_pool = (
            False  # disable flattening if pooling is pass-through (no pooling)
        )
    global_pool = SelectAdaptivePool2d(pool_type=pool_type, flatten=flatten_in_pool)
    num_pooled_features = num_features * global_pool.feat_mult()
    return global_pool, num_pooled_features

def random_normal(stddev):
    def random_normal_initializer(tensor):
        with torch.no_grad():
            tensor.normal_(mean=0, std=stddev)
    
    return random_normal_initializer

def orthogonal_random_features(stddev):
    def orthogonal_random_features_initializer(tensor):
        num_rows, num_cols = tensor.shape
        if num_rows < num_cols:
            # When num_rows < num_cols, sample multiple (num_rows, num_rows) matrices and
            # then concatenate.
            ortho_mat_list = []
            num_cols_sampled = 0

            while num_cols_sampled < num_cols:
                matrix = torch.empty_like(tensor[:, :num_rows])
                ortho_mat_square = nn.init.orthogonal_(
                    matrix
                )
                ortho_mat_list.append(ortho_mat_square)
                num_cols_sampled += num_rows
            
            # Reshape the matrix to the target shape (num_rows, num_cols)
            ortho_mat = torch.cat(ortho_mat_list, dim=-1)
            ortho_mat = ortho_mat[:, :num_cols]
        else:
            matrix = torch.empty_like(tensor)
            ortho_mat = nn.init.orthogonal_(matrix)
        
        # Sample random feature norms.
        # Construct Monte-Carlo estimate of squared column norm of a random
        # Gaussian matrix.
        feature_norms_square = torch.randn_like(ortho_mat)**2
        feature_norms = feature_norms_square.sum(dim=0)
        feature_norms = feature_norms.sqrt()

        # Sets a random feature matrix with orthogonal column and Gaussian-like
        # column norms.
        with torch.no_grad():
            tensor.data = ortho_mat * feature_norms
    
    return orthogonal_random_features_initializer

def make_random_feature_initializer(random_feature_type):
    # Use stddev=0.05 to replicate the default behavior of
    # tf.keras.initializer.RandomNormal.
    if random_feature_type == "orf":
        return orthogonal_random_features(stddev=0.05)
    elif random_feature_type == "rff":
        return random_normal(stddev=0.05)
    else:
        raise ValueError("Invalid random feature type provided.")


def _create_fc(
    num_features,
    num_classes,
    gp_hidden_dim,
    gp_scale,
    gp_bias,
    gp_input_normalization,
    gp_random_feature_type,
    gp_cov_discount_factor,
    gp_cov_ridge_penalty,
    gp_output_imagenet_initializer,
    use_conv=False,
    use_SNGP=False,
):
    if use_SNGP:
        if gp_output_imagenet_initializer:
            gp_output_initializer = random_normal(stddev=0.01)

        fc = RandomFeatureGaussianProcess(
            input_shape=num_features,
            units=num_classes,
            num_inducing=gp_hidden_dim,
            gp_kernel_scale=gp_scale,
            gp_output_bias=gp_bias,
            normalize_input=gp_input_normalization,
            gp_cov_momentum=gp_cov_discount_factor,
            gp_cov_ridge_penalty=gp_cov_ridge_penalty,
            custom_random_features_initializer=make_random_feature_initializer(
                gp_random_feature_type
            ),
            kernel_initializer=gp_output_initializer
        )
    elif num_classes <= 0:
        fc = nn.Identity()  # pass-through (no classifier)
    elif use_conv:
        fc = nn.Conv2d(num_features, num_classes, 1, bias=True)
    else:
        fc = nn.Linear(num_features, num_classes, bias=True)
    return fc


def create_classifier(
    num_features,
    num_classes,
    gp_hidden_dim=None,
    gp_scale=None,
    gp_bias=None,
    gp_input_normalization=None,
    gp_random_feature_type=None,
    gp_cov_discount_factor=None,
    gp_cov_ridge_penalty=None,
    gp_output_imagenet_initializer=None,
    pool_type="avg",
    use_conv=False,
    use_SNGP=False,
):
    global_pool, num_pooled_features = _create_pool(
        num_features, num_classes, pool_type, use_conv=use_conv
    )
    fc = _create_fc(
        num_pooled_features,
        num_classes,
        use_conv=use_conv,
        use_SNGP=use_SNGP,
        gp_hidden_dim=gp_hidden_dim,
        gp_scale=gp_scale,
        gp_bias=gp_bias,
        gp_input_normalization=gp_input_normalization,
        gp_random_feature_type=gp_random_feature_type,
        gp_cov_discount_factor=gp_cov_discount_factor,
        gp_cov_ridge_penalty=gp_cov_ridge_penalty,
        gp_output_imagenet_initializer=gp_output_imagenet_initializer,
    )
    return global_pool, fc


class ClassifierHead(nn.Module):
    """Classifier head w/ configurable global pooling and dropout."""

    def __init__(
        self,
        in_features: int,
        num_classes: int,
        pool_type: str = "avg",
        drop_rate: float = 0.0,
        use_conv: bool = False,
    ):
        """
        Args:
            in_features: The number of input features.
            num_classes:  The number of classes for the final classifier layer (output).
            pool_type: Global pooling type, pooling disabled if empty string ('').
            drop_rate: Pre-classifier dropout rate.
        """
        super(ClassifierHead, self).__init__()
        self.drop_rate = drop_rate
        self.in_features = in_features
        self.use_conv = use_conv

        self.global_pool, num_pooled_features = _create_pool(
            in_features, num_classes, pool_type, use_conv=use_conv
        )
        self.fc = _create_fc(num_pooled_features, num_classes, use_conv=use_conv)
        self.flatten = nn.Flatten(1) if use_conv and pool_type else nn.Identity()

    def reset(self, num_classes, global_pool=None):
        if global_pool is not None:
            if global_pool != self.global_pool.pool_type:
                self.global_pool, _ = _create_pool(
                    self.in_features, num_classes, global_pool, use_conv=self.use_conv
                )
            self.flatten = (
                nn.Flatten(1) if self.use_conv and global_pool else nn.Identity()
            )
        num_pooled_features = self.in_features * self.global_pool.feat_mult()
        self.fc = _create_fc(num_pooled_features, num_classes, use_conv=self.use_conv)

    def forward(self, x, pre_logits: bool = False):
        x = self.global_pool(x)
        if self.drop_rate:
            x = F.dropout(x, p=float(self.drop_rate), training=self.training)
        if pre_logits:
            return x.flatten(1)
        else:
            x = self.fc(x)
            return self.flatten(x)


class NormMlpClassifierHead(nn.Module):
    def __init__(
        self,
        in_features: int,
        num_classes: int,
        hidden_size: Optional[int] = None,
        pool_type: str = "avg",
        drop_rate: float = 0.0,
        norm_layer: Union[str, Callable] = "layernorm2d",
        act_layer: Union[str, Callable] = "tanh",
    ):
        """
        Args:
            in_features: The number of input features.
            num_classes:  The number of classes for the final classifier layer (output).
            hidden_size: The hidden size of the MLP (pre-logits FC layer) if not None.
            pool_type: Global pooling type, pooling disabled if empty string ('').
            drop_rate: Pre-classifier dropout rate.
            norm_layer: Normalization layer type.
            act_layer: MLP activation layer type (only used if hidden_size is not None).
        """
        super().__init__()
        self.drop_rate = drop_rate
        self.in_features = in_features
        self.hidden_size = hidden_size
        self.num_features = in_features
        self.use_conv = not pool_type
        norm_layer = get_norm_layer(norm_layer)
        act_layer = get_act_layer(act_layer)
        linear_layer = partial(nn.Conv2d, kernel_size=1) if self.use_conv else nn.Linear

        self.global_pool = SelectAdaptivePool2d(pool_type=pool_type)
        self.norm = norm_layer(in_features)
        self.flatten = nn.Flatten(1) if pool_type else nn.Identity()
        if hidden_size:
            self.pre_logits = nn.Sequential(
                OrderedDict(
                    [
                        ("fc", linear_layer(in_features, hidden_size)),
                        ("act", act_layer()),
                    ]
                )
            )
            self.num_features = hidden_size
        else:
            self.pre_logits = nn.Identity()
        self.drop = nn.Dropout(self.drop_rate)
        self.fc = (
            linear_layer(self.num_features, num_classes)
            if num_classes > 0
            else nn.Identity()
        )

    def reset(self, num_classes, global_pool=None):
        if global_pool is not None:
            self.global_pool = SelectAdaptivePool2d(pool_type=global_pool)
            self.flatten = nn.Flatten(1) if global_pool else nn.Identity()
        self.use_conv = self.global_pool.is_identity()
        linear_layer = partial(nn.Conv2d, kernel_size=1) if self.use_conv else nn.Linear
        if self.hidden_size:
            if (isinstance(self.pre_logits.fc, nn.Conv2d) and not self.use_conv) or (
                isinstance(self.pre_logits.fc, nn.Linear) and self.use_conv
            ):
                with torch.no_grad():
                    new_fc = linear_layer(self.in_features, self.hidden_size)
                    new_fc.weight.copy_(
                        self.pre_logits.fc.weight.reshape(new_fc.weight.shape)
                    )
                    new_fc.bias.copy_(self.pre_logits.fc.bias)
                    self.pre_logits.fc = new_fc
        self.fc = (
            linear_layer(self.num_features, num_classes)
            if num_classes > 0
            else nn.Identity()
        )

    def forward(self, x, pre_logits: bool = False):
        x = self.global_pool(x)
        x = self.norm(x)
        x = self.flatten(x)
        x = self.pre_logits(x)
        if pre_logits:
            return x
        x = self.fc(x)
        return x
