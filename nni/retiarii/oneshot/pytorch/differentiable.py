from collections import OrderedDict
import torch
import torch.nn as nn
import torch.nn.functional as F

from .base_lightning import BaseOneShotLightningModule
from nni.retiarii.nn.pytorch import LayerChoice, InputChoice

class DartsLayerChoice(nn.Module):
    def __init__(self, layer_choice):
        super(DartsLayerChoice, self).__init__()
        self.name = layer_choice.label
        self.op_choices = nn.ModuleDict(OrderedDict([(name, layer_choice[name]) for name in layer_choice.names]))
        self.alpha = nn.Parameter(torch.randn(len(self.op_choices)) * 1e-3)

    def forward(self, *args, **kwargs):
        op_results = torch.stack([op(*args, **kwargs) for op in self.op_choices.values()])
        alpha_shape = [-1] + [1] * (len(op_results.size()) - 1)
        return torch.sum(op_results * F.softmax(self.alpha, -1).view(*alpha_shape), 0)

    def parameters(self):
        for _, p in self.named_parameters():
            yield p

    def named_parameters(self, recurse):
        for name, p in super(DartsLayerChoice, self).named_parameters():
            if name == 'alpha':
                continue
            yield name, p

    def export(self):
        return list(self.op_choices.keys())[torch.argmax(self.alpha).item()]
    
    def r_forward(self, x):
        return self.op_choices.values()[0](x)

class DartsInputChoice(nn.Module):
    def __init__(self, input_choice):
        super(DartsInputChoice, self).__init__()
        self.name = input_choice.label
        self.alpha = nn.Parameter(torch.randn(input_choice.n_candidates) * 1e-3)
        self.n_chosen = input_choice.n_chosen or 1

    def forward(self, inputs):
        inputs = torch.stack(inputs)
        alpha_shape = [-1] + [1] * (len(inputs.size()) - 1)
        return torch.sum(inputs * F.softmax(self.alpha, -1).view(*alpha_shape), 0)

    def parameters(self):
        for _, p in self.named_parameters():
            yield p

    def named_parameters(self, recurse):
        for name, p in super(DartsInputChoice, self).named_parameters():
            if name == 'alpha':
                continue
            yield name, p

    def export(self):
        return torch.argsort(-self.alpha).cpu().numpy().tolist()[:self.n_chosen]
    
    def r_forward(self, x):
        return super().r_forward(x)

DARTS_REPLACE_DICT = {
    LayerChoice : DartsLayerChoice,
    InputChoice : DartsInputChoice
}

class DartsModel(BaseOneShotLightningModule):
    '''
        choice_replace_dict 是一个 xxxChoice 类到用户自己实现的 ChoiceReplace 的类的字典。可以改个名字。
    '''
    def __init__(self, model, custom_replace_dict = None):
        super().__init__(model, custom_replace_dict)
        self.automatic_optimization = False
    
    def on_train_start(self) -> None:
        return super().on_train_start()
        
    def training_step(self, batch, batch_idx):
        # grad manually
        opts = self.optimizers()
        arc_optim = opts[0]
        w_optim = opts[1:]

        # MergeTrainValDataset will yield both train and val data in a batch
        trn_batch, val_batch = batch

        # phase 1. architecture step
        self.resample_architecture()
        arc_optim.zero_grad()
        arc_step_loss = self.model.training_step(val_batch, 2 * batch_idx)
        self.manual_backward(arc_step_loss)
        arc_optim.step()

        # phase 2: child network step
        self.resample_architecture()
        for opt in w_optim:
            opt.zero_grad()
        w_step_loss = self.model.training_step(trn_batch, 2 * batch_idx + 1)
        self.manual_backward(w_step_loss)
        for opt in w_optim:
            opt.step()
        return w_step_loss

    def validation_step(self, batch, batch_idx):
        return

    def resample_architecture(self):
        pass
    
    @property
    def default_replace_dict(self):
        return DARTS_REPLACE_DICT

    def configure_architecture_optimizers(self):
        ctrl_params = {}
        for _, m in self.nas_modules:
            if m.name in ctrl_params:
                assert m.alpha.size() == ctrl_params[m.name].size(), 'Size of parameters with the same label should be same.'
                m.alpha = ctrl_params[m.name]
            else:
                ctrl_params[m.name] = m.alpha
        ctrl_optim = torch.optim.Adam(list(ctrl_params.values()), 3.e-4, betas=(0.5, 0.999),
                                           weight_decay=1.0E-3)
        return ctrl_optim

class _ArchGradientFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, binary_gates, run_func, backward_func):
        ctx.run_func = run_func
        ctx.backward_func = backward_func
 
        detached_x = x.detach()
        detached_x.requires_grad = x.requires_grad
        with torch.enable_grad():
            output = run_func(detached_x)
        ctx.save_for_backward(detached_x, output)
        return output.data
 
    @staticmethod
    def backward(ctx, grad_output):
        detached_x, output = ctx.saved_tensors
 
        grad_x = torch.autograd.grad(output, detached_x, grad_output, only_inputs=True)
        # compute gradients w.r.t. binary_gates
        binary_grads = ctx.backward_func(detached_x.data, output.data, grad_output.data)
 
        return grad_x[0], binary_grads, None, None
     
class ProxylessLayerChoice(nn.Module):
    def __init__(self, ops):
        super(ProxylessLayerChoice, self).__init__()
        self.ops = nn.ModuleList(ops)
        self.alpha = nn.Parameter(torch.randn(len(self.ops)) * 1E-3)
        self._binary_gates = nn.Parameter(torch.randn(len(self.ops)) * 1E-3)
        self.sampled = None

    def forward(self, *args, **kwargs):
        if self.training:
            def run_function(ops, active_id, **kwargs):
                def forward(_x):
                    return ops[active_id](_x, **kwargs)
                return forward

            def backward_function(ops, active_id, binary_gates, **kwargs):
                def backward(_x, _output, grad_output):
                    binary_grads = torch.zeros_like(binary_gates.data)
                    with torch.no_grad():
                        for k in range(len(ops)):
                            if k != active_id:
                                out_k = ops[k](_x.data, **kwargs)
                            else:
                                out_k = _output.data
                            grad_k = torch.sum(out_k * grad_output)
                            binary_grads[k] = grad_k
                    return binary_grads
                return backward

            assert len(args) == 1
            x = args[0]
            return _ArchGradientFunction.apply(
                x, self._binary_gates, run_function(self.ops, self.sampled, **kwargs),
                backward_function(self.ops, self.sampled, self._binary_gates, **kwargs)
            )
        
        return super().forward(*args, **kwargs)

    def resample(self):
        probs = F.softmax(self.alpha, dim=-1)
        sample = torch.multinomial(probs, 1)[0].item()
        self.sampled = sample
        with torch.no_grad():
            self._binary_gates.zero_()
            self._binary_gates.grad = torch.zeros_like(self._binary_gates.data)
            self._binary_gates.data[sample] = 1.0

    def finalize_grad(self):
        binary_grads = self._binary_gates.grad
        with torch.no_grad():
            if self.alpha.grad is None:
                self.alpha.grad = torch.zeros_like(self.alpha.data)
            probs = F.softmax(self.alpha, dim=-1)
            for i in range(len(self.ops)):
                for j in range(len(self.ops)):
                    self.alpha.grad[i] += binary_grads[j] * probs[j] * (int(i == j) - probs[i])

    def export(self):
        return torch.argmax(self.alpha).item()

    def export_prob(self):
        return F.softmax(self.alpha, dim=-1)

class ProxylessInputChoice(nn.Module):
    def __init__(self, input_choice):
        super().__init__()
        self.ops = nn.ModuleList(input_choice)
        self.alpha = nn.Parameter(torch.randn(input_choice.n_candidates) * 1E-3)
        self._binary_gates = nn.Parameter(torch.randn(input_choice.n_candidates) * 1E-3)
        self.sampled = None

    def forward(self, inputs):
        if self.training:
            def run_function(active_sample):
                return lambda x: x[active_sample]

            def backward_function(binary_gates):
                def backward(_x, _output, grad_output):
                    binary_grads = torch.zeros_like(binary_gates.data)
                    with torch.no_grad():
                        for k in range(self.num_input_candidates):
                            out_k = _x[k].data
                            grad_k = torch.sum(out_k * grad_output)
                            binary_grads[k] = grad_k
                    return binary_grads
                return backward

            inputs = torch.stack(inputs, 0)
            return _ArchGradientFunction.apply(
                inputs, self._binary_gates, run_function(self.sampled),
                backward_function(self._binary_gates)
            )
        
        return super().forward(inputs)


    def resample(self, sample=None):
        if sample is None:
            probs = F.softmax(self.alpha, dim=-1)
            sample = torch.multinomial(probs, 1)[0].item()
        self.sampled = sample
        with torch.no_grad():
            self._binary_gates.zero_()
            self._binary_gates.grad = torch.zeros_like(self._binary_gates.data)
            self._binary_gates.data[sample] = 1.0
        return self.sampled

    def finalize_grad(self):
        binary_grads = self._binary_gates.grad
        with torch.no_grad():
            if self.alpha.grad is None:
                self.alpha.grad = torch.zeros_like(self.alpha.data)
            probs = F.softmax(self.alpha, dim=-1)
            for i in range(self.num_input_candidates):
                for j in range(self.num_input_candidates):
                    self.alpha.grad[i] += binary_grads[j] * probs[j] * (int(i == j) - probs[i])

PROXYLESS_REPLACE_DICT = {
    LayerChoice : ProxylessLayerChoice,
    InputChoice : ProxylessInputChoice
}

class ProxylessModel(DartsModel):
    def __init__(self, base_model, custom_replace_dict = None):
        super().__init__(base_model, custom_replace_dict)

    
    def configure_architecture_optimizers(self):
        ctrl_optim = torch.optim.Adam([m.alpha for _, m in self.nas_modules], 3.e-4,
                                           weight_decay=0, betas=(0, 0.999), eps=1e-8)
        return ctrl_optim
    
    @property
    def default_replace_dict(self):
        return PROXYLESS_REPLACE_DICT
    
    def resample_architecture(self):
        for _, m in self.nas_modules:
            m.resample()
