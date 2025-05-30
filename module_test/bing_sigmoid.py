import torch
from torch import nn
from torch.nn.parameter import Parameter

import math
import torch.nn.functional as F


class eca_layer(nn.Module):
    """Constructs a ECA module.

    Args:
        channel: Number of channels of the input feature map
        k_size: Adaptive selection of kernel size
    """

    def __init__(self, channel, k_size=3):
        super(eca_layer, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.conv = nn.Conv1d(1, 1, kernel_size=k_size, padding=(k_size - 1) // 2, bias=False)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # feature descriptor on the global spatial information
        y = self.avg_pool(x)

        # Two different branches of ECA module
        y = self.conv(y.squeeze(-1).transpose(-1, -2)).transpose(-1, -2).unsqueeze(-1)

        # Multi-scale information fusion
        y = self.sigmoid(y)

        return x * y.expand_as(x)


# if __name__ == '__main__':
#     input = torch.randn(3, 64, 256, 256)
#     model = eca_layer(64)
#     out = model(input)
#     print(out.shape)


class BasicConv(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1, groups=1, relu=True,
                 bn=True, bias=False):
        super(BasicConv, self).__init__()
        self.out_channels = out_planes
        self.conv = nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=padding,
                              dilation=dilation, groups=groups, bias=bias)
        self.bn = nn.BatchNorm2d(out_planes, eps=1e-5, momentum=0.01, affine=True) if bn else None
        self.relu = nn.ReLU() if relu else None

    def forward(self, x):
        x = self.conv(x)
        if self.bn is not None:
            x = self.bn(x)
        if self.relu is not None:
            x = self.relu(x)
        return x


class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)


class ChannelGate(nn.Module):
    def __init__(self, gate_channels, reduction_ratio=16, pool_types=['avg', 'max']):
        super(ChannelGate, self).__init__()
        self.gate_channels = gate_channels
        self.mlp = nn.Sequential(
            Flatten(),
            nn.Linear(gate_channels, gate_channels // reduction_ratio),
            nn.ReLU(),
            nn.Linear(gate_channels // reduction_ratio, gate_channels)
        )
        self.pool_types = pool_types

    def forward(self, x):
        channel_att_sum = None
        for pool_type in self.pool_types:
            if pool_type == 'avg':
                avg_pool = F.avg_pool2d(x, (x.size(2), x.size(3)), stride=(x.size(2), x.size(3)))
                channel_att_raw = self.mlp(avg_pool)
            elif pool_type == 'max':
                max_pool = F.max_pool2d(x, (x.size(2), x.size(3)), stride=(x.size(2), x.size(3)))
                channel_att_raw = self.mlp(max_pool)
            elif pool_type == 'lp':
                lp_pool = F.lp_pool2d(x, 2, (x.size(2), x.size(3)), stride=(x.size(2), x.size(3)))
                channel_att_raw = self.mlp(lp_pool)
            elif pool_type == 'lse':
                # LSE pool only
                lse_pool = logsumexp_2d(x)
                channel_att_raw = self.mlp(lse_pool)

            if channel_att_sum is None:
                channel_att_sum = channel_att_raw
            else:
                channel_att_sum = channel_att_sum + channel_att_raw

        scale = F.sigmoid(channel_att_sum).unsqueeze(2).unsqueeze(3).expand_as(x)
        return x * scale


def logsumexp_2d(tensor):
    tensor_flatten = tensor.view(tensor.size(0), tensor.size(1), -1)
    s, _ = torch.max(tensor_flatten, dim=2, keepdim=True)
    outputs = s + (tensor_flatten - s).exp().sum(dim=2, keepdim=True).log()
    return outputs


class ChannelPool(nn.Module):
    def forward(self, x):
        return torch.cat((torch.max(x, 1)[0].unsqueeze(1), torch.mean(x, 1).unsqueeze(1)), dim=1)


class SpatialGate(nn.Module):
    def __init__(self):
        super(SpatialGate, self).__init__()
        kernel_size = 7
        self.compress = ChannelPool()
        self.spatial = BasicConv(2, 1, kernel_size, stride=1, padding=(kernel_size - 1) // 2, relu=False)

    def forward(self, x):
        x_compress = self.compress(x)
        x_out = self.spatial(x_compress)
        scale = F.sigmoid(x_out)  # broadcasting
        return x * scale


class CBAM(nn.Module):
    def __init__(self, gate_channels, reduction_ratio=16, pool_types=['avg', 'max'], no_spatial=False):
        super(CBAM, self).__init__()
        self.ChannelGate = ChannelGate(gate_channels, reduction_ratio, pool_types)
        self.no_spatial = no_spatial

        self.eca = eca_layer(gate_channels)
        self.conv = nn.Conv2d(gate_channels * 2, gate_channels, kernel_size=1, stride=1)
        self.sigmoid = nn.Sigmoid()
        self.tanh = nn.Tanh()

        if not no_spatial:
            self.SpatialGate = SpatialGate()

    # # 替换ChannelGate(x)
    # def forward(self, x):
    #     # x_out = self.ChannelGate(x)
    #     x_out = self.eca(x)
    #     if not self.no_spatial:
    #         x_out = self.SpatialGate(x_out)
    #     return x_out

    # # 串行：ChannelGate -> eca -> SpatialGate
    # def forward(self, x):
    #     x_out = self.ChannelGate(x)
    #     x_out = self.eca(x_out)
    #     if not self.no_spatial:
    #         x_out = self.SpatialGate(x_out)
    #     return x_out

    # # 串行：ChannelGate -> eca -> SpatialGate
    # def forward(self, x):
    #     # 替换ChannelGate(x)
    #     x_out = self.ChannelGate(x)
    #     x_out = self.eca(x_out) + x
    #     if not self.no_spatial:
    #         x_out = self.SpatialGate(x_out) + x
    #     return x_out

    # # 串行：eca -> ChannelGate -> SpatialGate
    # def forward(self, x):
    #     x_out = self.ChannelGate(x)
    #     x_out = self.eca(x_out)
    #     if not self.no_spatial:
    #         x_out = self.SpatialGate(x_out)
    #     return x_out

    # # 并行+：eca + ChannelGate -> SpatialGate
    # def forward(self, x):
    #     x_out1 = self.ChannelGate(x)
    #     x_out2 = self.eca(x)
    #     x_out = x_out1 + x_out2
    #     if not self.no_spatial:
    #         x_out = self.SpatialGate(x_out)
    #     return x_out

    # # 并行(torch.cat)：eca + ChannelGate -> SpatialGate
    # def forward(self, x):
    #     x_out1 = self.ChannelGate(x)
    #     x_out2 = self.eca(x)
    #     x_out = torch.cat([x_out1, x_out2], dim=1)
    #     x_out = self.conv(x_out)
    #     if not self.no_spatial:
    #         x_out = self.SpatialGate(x_out)
    #     return x_out

    # 并行(门控机制)：eca + ChannelGate -> SpatialGate
    def forward(self, x):
        x_out1 = self.ChannelGate(x)
        x_out2 = self.eca(x)

        X1 = self.sigmoid(x_out1)
        x_out = X1 * x_out2

        if not self.no_spatial:
            x_out = self.SpatialGate(x_out)
        return x_out

if __name__ == '__main__':
    input = torch.randn(3, 64, 256, 256)
    model = CBAM(64)
    out = model(input)
    print(out.shape)

