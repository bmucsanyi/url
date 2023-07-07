"""
Contains methods to turn each model into a model that also returns uncertainty estimates.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from timm.models.resnet import ResNetDropout
from timm.models.vision_transformer import VisionTransformerDropout

class ModelWrapper(nn.Module):
    """
    This module takes a model as input and then performs all possible functions on the model's functions.
    Children of the ModelWrapper class will be allowed to overwrite these functions.
    This "dirty" implementation is because we do not know which class our given model will have,
    so we cannot just be a subclass of it.
    If this does not work, we could go even more dirty and replace the forward functions at inference time:
    https://discuss.pytorch.org/t/how-can-i-replace-the-forward-method-of-a-predefined-torchvision-model-with-my-customized-forward-function/54224/11
    """

    def __init__(self, model) -> None:
        super().__init__()
        self.model = model
        self.num_classes = model.num_classes
        if hasattr(model, "drop_rate"):
            self.drop_rate = model.drop_rate
        self.grad_checkpointing = model.grad_checkpointing
        self.num_features = model.num_features

    @torch.jit.ignore
    def group_matcher(self, *args, **kwargs):
        return self.model.group_matcher(*args, **kwargs)

    @torch.jit.ignore
    def set_grad_checkpointing(self, *args, **kwargs):
        res = self.model.set_grad_checkpointing(*args, **kwargs)
        self.grad_checkpointing = self.model.grad_checkpointing
        return res

    @torch.jit.ignore
    def get_classifier(self, *args, **kwargs):
        return self.model.get_classifier(*args, **kwargs)

    def reset_classifier(self, *args, **kwargs):
        return self.model.reset_classifier(*args, **kwargs)

    def forward_features(self, *args, **kwargs):
        return self.model.forward_features(*args, **kwargs)

    def forward_head(self, *args, **kwargs):
        return self.model.forward_head(*args, **kwargs)

    def forward(self, *args, **kwargs):
        return self.model.forward(*args, **kwargs)


class ShallowEnsembleClassifier(nn.Module):
    def __init__(
        self, num_heads, num_features, num_classes
    ) -> None:
        super().__init__()
        self.shallow_classifiers = nn.Linear(num_features, num_classes * num_heads)
        self.num_heads = num_heads
        self.num_classes = num_classes

    def forward(self, x):
        logits = self.shallow_classifiers(x).reshape(-1, self.num_heads, self.num_classes)  # [B, N, C]
        return logits.transpose(0, 1)


class ShallowEnsembleWrapper(ModelWrapper):
    """
    This module takes a model as input and creates a shallow ensemble from it.
    """

    def __init__(self, model, num_heads) -> None:
        super().__init__(model)
        # WARNING: self.num_features fails with catavgmax
        # There, pooling doubles feature dims so this
        # ensemble head results in a shape error
        self.classifier = ShallowEnsembleClassifier(
            num_heads, self.num_features, self.num_classes
        )
        self.num_heads = num_heads

    @torch.jit.ignore
    def get_classifier(self):
        return self.classifier

    def reset_classifier(self, num_heads=None, *args, **kwargs):
        if num_heads is None:
            num_heads = self.num_heads
        # Resets global pooling in `self.classifier`
        self.model.reset_classifier(*args, **kwargs)
        self.num_classes = self.model.num_classes
        self.classifier = ShallowEnsembleClassifier(
            num_heads, self.num_features, self.num_classes
        )

    def forward_features(self, *args, **kwargs):
        # No change via ensembling
        return self.model.forward_features(*args, **kwargs)

    def forward_head(self, x, pre_logits: bool = False):
        # Always get pre_logits
        x = self.model.forward_head(x, pre_logits=True)

        # Optionally apply `self.classifier`
        return x if pre_logits else self.classifier(x)

    def forward(self, x):
        x = self.forward_features(x)
        x = self.forward_head(x)
        return x


class UncertaintyWrapper(ModelWrapper):
    def __init__(self, model) -> None:
        super().__init__(model)
        self.unc_scaler = 1.0

    def initialize_avg_uncertainty(self, loader_train, target_avg_unc, n_batches=10):
        # Find out which uncertainty the model currently predicts on average
        avg_unc = 0.0
        data_iter = loader_train.__iter__()
        prev_state = self.training
        self.eval()
        with torch.no_grad():
            for _ in range(n_batches):
                input, _ = data_iter.__next__()
                _, unc, _ = self(input)
                avg_unc += unc.mean().detach().cpu().item() / n_batches
        
        self.train(prev_state)

        # Match our unc_scaler to meet the target_avg_unc
        self.unc_scaler = target_avg_unc / avg_unc

    def freeze_backbone(self):
        for param in self.model.parameters():
            param.requires_grad = False


class UncertaintyViaNorm(UncertaintyWrapper):
    def __init__(self, model) -> None:
        super().__init__(model)

    def forward(self, *args, **kwargs):
        # In addition to whatever the model itself outputs (usually classes) (Tensor of shape [Batchsize, Classes])
        # also output an uncertainty estimate based on the norm of the embedding (Tensor of shape [Batchsize])
        features = self.model.forward_head(self.model.forward_features(*args, **kwargs), pre_logits=True)
        unc = 1 / features.norm(dim=-1)  # norms = certainty, so return their inverse
        unc = unc * self.unc_scaler
        out = self.model.get_classifier()(features)

        return out, unc, features

class UncertaintyViaJSD(UncertaintyWrapper):
    def __init__(self, model) -> None:
        super().__init__(model)

    def forward(self, *args, **kwargs):
        if (isinstance(self.model, ResNetDropout) or isinstance(self.model, VisionTransformerDropout)) and not self.training:
            # Test-time dropout
            predictions = []
            for _ in range(self.model.num_dropout_samples):
                features = self.model.forward_head(self.model.forward_features(*args, **kwargs), pre_logits=True)
                logits = self.model.get_classifier()(features)  # [B, C]
                predictions.append(logits.unsqueeze(0))

            # Stack predictions
            predictions = torch.cat(predictions, dim=0)  # [S, B, C]

            unc, out = get_unc_out(predictions, self.unc_scaler)
        elif isinstance(self.model, ShallowEnsembleWrapper):
            features = self.model.forward_head(self.model.forward_features(*args, **kwargs), pre_logits=True)
            predictions = self.model.get_classifier()(features)  # [S, B, C]

            unc, out = get_unc_out(predictions, self.unc_scaler)
        else:
            if not (isinstance(self.model, ResNetDropout) or isinstance(self.model, VisionTransformerDropout)):
                raise ValueError(
                    f"Model has type {type(self.model)} but expected `ResNetDropout`"
                    ", ShallowEnsembleWrapper, or VisionTransformerDropout."
                )
            # In addition to whatever the model itself outputs (usually classes) (Tensor of shape [Batchsize, Classes])
            # also output an uncertainty estimate based on the JSD of the class distribution (Tensor of shape [Batchsize])
            features = self.model.forward_head(self.model.forward_features(*args, **kwargs), pre_logits=True)
            out = self.model.get_classifier()(features)  # [B, C]
            class_prob = out.softmax(dim=-1)

            # Only calculate the entropy during training as a substitute uncertainty
            # value for dropout nets
            unc = entropy(class_prob)  # [B]
            unc = unc * self.unc_scaler

        return out, unc, features

class UncertaintyViaEntropy(UncertaintyWrapper):
    def __init__(self, model) -> None:
        super().__init__(model)

    def forward(self, *args, **kwargs):
        if (isinstance(self.model, ResNetDropout) or isinstance(self.model, VisionTransformerDropout)) and not self.training:
            # Test-time dropout
            predictions = []
            for _ in range(self.model.num_dropout_samples):
                features = self.model.forward_head(self.model.forward_features(*args, **kwargs), pre_logits=True)
                logits = self.model.get_classifier()(features)  # [B, C]
                predictions.append(logits.unsqueeze(0))

            # Stack predictions
            predictions = torch.cat(predictions, dim=0)  # [S, B, C]

            # Apply averaging
            out = F.softmax(predictions, dim=-1).mean(dim=0).log()
        elif isinstance(self.model, ShallowEnsembleWrapper):
            features = self.model.forward_head(self.model.forward_features(*args, **kwargs), pre_logits=True)
            predictions = self.model.get_classifier()(features)  # [S, B, C]
            probs = F.softmax(predictions, dim=-1)  # [S, B, C]
            mean_probs = probs.mean(dim=0)  # [B, C]
            out = mean_probs.log()
        else:
            # In addition to whatever the model itself outputs (usually classes) (Tensor of shape [Batchsize, Classes])
            # also output an uncertainty estimate based on the entropy of the class distribution (Tensor of shape [Batchsize])
            features = self.model.forward_head(self.model.forward_features(*args, **kwargs), pre_logits=True)
            out = self.model.get_classifier()(features)
        class_prob = out.softmax(dim=-1)

        entr = entropy(class_prob)
        entr = entr * self.unc_scaler

        return out, entr, features


class UncertaintyViaConst(UncertaintyWrapper):
    def __init__(self, model) -> None:
        super().__init__(model)

    def forward(self, *args, **kwargs):
        # In addition to whatever the model itself outputs (usually classes) (Tensor of shape [Batchsize, Classes])
        # also output a constant uncertainty estimate (acting as baseline) (Tensor of shape [Batchsize])
        features = self.model.forward_head(self.model.forward_features(*args, **kwargs), pre_logits=True)
        out = self.model.get_classifier()(features)
        unc = torch.ones(out.shape[0], device=out.device)
        unc = unc * self.unc_scaler

        return out, unc, features


class UncertaintyViaNetwork(UncertaintyWrapper):
    def __init__(self, model, *args, **kwargs):
        super().__init__(model)
        self.unc_module = UncertaintyNetwork(
            in_channels=model.num_features, *args, **kwargs
        )

    def forward(self, *args, **kwargs):
        features = self.model.forward_head(self.model.forward_features(*args, **kwargs), pre_logits=True)
        out = self.model.get_classifier()(features)
        unc = self.unc_module(features).squeeze()
        unc = unc * self.unc_scaler

        return out, unc, features

class UncertaintyViaHETXLCov(UncertaintyWrapper):
    def __init__(self, model):
        super().__init__(model)

    def forward(self, *args, **kwargs):
        features = self.model.forward_head(self.model.forward_features(*args, **kwargs), pre_logits=True)
        out, unc = self.model.get_classifier()(features, calc_cov_log_det=True)
        unc = unc * self.unc_scaler

        return out, unc, features


class UncertaintyNetwork(nn.Module):
    def __init__(self, in_channels=2048, width=512) -> None:
        super().__init__()
        self.unc_module = nn.Sequential(
            nn.Linear(in_channels, width),
            nn.LeakyReLU(),
            nn.Linear(width, width),
            nn.LeakyReLU(),
            nn.Linear(width, width),
            nn.LeakyReLU(),
            nn.Linear(width, 1),
            nn.Softplus(),
        )
        self.EPS = 1e-6

    def forward(self, input):
        return self.EPS + self.unc_module(input)

def entropy(probs):
    log_probs = probs.log()
    min_real = torch.finfo(log_probs.dtype).min
    log_probs = torch.clamp(log_probs, min=min_real)
    p_log_p = log_probs * probs
    
    return -p_log_p.sum(dim=-1)

def get_unc_out(predictions, unc_scaler):
    probs = F.softmax(predictions, dim=-1)  # [S, B, C]
    mean_probs = probs.mean(dim=0)  # [B, C]
    entropy_of_mean = entropy(mean_probs)  # [B]
    mean_of_entropy = entropy(probs).mean(dim=0)  # [B]

    unc = entropy_of_mean - mean_of_entropy
    unc = unc * unc_scaler

    # Apply averaging
    out = mean_probs.log()

    return unc, out