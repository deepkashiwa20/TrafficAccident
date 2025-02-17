'''
ASTGCN
'''
import sys
import math
import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
import pandas as pd
from scipy.sparse.linalg import eigs
from torchsummary import summary

class Spatial_Attention_layer(nn.Module):
    '''
    compute spatial attention scores
    '''
    def __init__(self, device, in_channels, num_of_vertices, num_of_timesteps):
        super(Spatial_Attention_layer, self).__init__()
        self.W1 = nn.Parameter(torch.FloatTensor(num_of_timesteps).to(device))
        self.W2 = nn.Parameter(torch.FloatTensor(in_channels, num_of_timesteps).to(device))
        self.W3 = nn.Parameter(torch.FloatTensor(in_channels).to(device))
        self.bs = nn.Parameter(torch.FloatTensor(1, num_of_vertices, num_of_vertices).to(device))
        self.Vs = nn.Parameter(torch.FloatTensor(num_of_vertices, num_of_vertices).to(device))

    def forward(self, x):
        '''
        :param x: (batch_size, N, F_in, T)
        :return: (B,N,N)
        '''
        lhs = torch.matmul(torch.matmul(x, self.W1), self.W2)  # (b,N,F,T)(T)->(b,N,F)(F,T)->(b,N,T)
        rhs = torch.matmul(self.W3, x).transpose(-1, -2)  # (F)(b,N,F,T)->(b,N,T)->(b,T,N)
        product = torch.matmul(lhs, rhs)  # (b,N,T) (b,T,N) -> (B, N, N)
        S = torch.matmul(self.Vs, torch.sigmoid(product + self.bs))  # (N,N)(B, N, N)->(B,N,N)
        S_normalized = F.softmax(S, dim=1)
        return S_normalized


class cheb_conv_withSAt(nn.Module):
    '''
    K-order chebyshev graph convolution
    '''

    def __init__(self, K, cheb_polynomials, in_channels, out_channels):
        '''
        :param K: int
        :param in_channles: int, num of channels in the input sequence
        :param out_channels: int, num of channels in the output sequence
        '''
        super(cheb_conv_withSAt, self).__init__()
        self.K = K
        self.cheb_polynomials = cheb_polynomials
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.device = cheb_polynomials[0].device
        self.Theta = nn.ParameterList([nn.Parameter(torch.FloatTensor(in_channels, out_channels).to(self.device)) for _ in range(K)])

    def forward(self, x, spatial_attention):
        '''
        Chebyshev graph convolution operation
        :param x: (batch_size, N, F_in, T)
        :return: (batch_size, N, F_out, T)
        '''
        batch_size, num_of_vertices, in_channels, num_of_timesteps = x.shape
        outputs = []

        for time_step in range(num_of_timesteps):
            graph_signal = x[:, :, :, time_step]  # (b, N, F_in)
            output = torch.zeros(batch_size, num_of_vertices, self.out_channels).to(self.device)  # (b, N, F_out)
            for k in range(self.K):
                T_k = self.cheb_polynomials[k]  # (N,N)
                T_k_with_at = T_k.mul(spatial_attention)   # (N,N)*(N,N) = (N,N) 多行和为1, 按着列进行归一化
                theta_k = self.Theta[k]  # (in_channel, out_channel)
                rhs = T_k_with_at.permute(0, 2, 1).matmul(graph_signal)  # (N, N)(b, N, F_in) = (b, N, F_in) 因为是左乘，所以多行和为1变为多列和为1，即一行之和为1，进行左乘
                output = output + rhs.matmul(theta_k)  # (b, N, F_in)(F_in, F_out) = (b, N, F_out)
            outputs.append(output.unsqueeze(-1))  # (b, N, F_out, 1)
        return F.relu(torch.cat(outputs, dim=-1))  # (b, N, F_out, T)


class Temporal_Attention_layer(nn.Module):
    def __init__(self, device, in_channels, num_of_vertices, num_of_timesteps):
        super(Temporal_Attention_layer, self).__init__()
        self.U1 = nn.Parameter(torch.FloatTensor(num_of_vertices).to(device))
        self.U2 = nn.Parameter(torch.FloatTensor(in_channels, num_of_vertices).to(device))
        self.U3 = nn.Parameter(torch.FloatTensor(in_channels).to(device))
        self.be = nn.Parameter(torch.FloatTensor(1, num_of_timesteps, num_of_timesteps).to(device))
        self.Ve = nn.Parameter(torch.FloatTensor(num_of_timesteps, num_of_timesteps).to(device))

    def forward(self, x):
        '''
        :param x: (batch_size, N, F_in, T)
        :return: (B, T, T)
        '''
        _, num_of_vertices, num_of_features, num_of_timesteps = x.shape

        lhs = torch.matmul(torch.matmul(x.permute(0, 3, 2, 1), self.U1), self.U2)
        # x:(B, N, F_in, T) -> (B, T, F_in, N)
        # (B, T, F_in, N)(N) -> (B,T,F_in)
        # (B,T,F_in)(F_in,N)->(B,T,N)
        rhs = torch.matmul(self.U3, x)  # (F)(B,N,F,T)->(B, N, T)
        product = torch.matmul(lhs, rhs)  # (B,T,N)(B,N,T)->(B,T,T)
        E = torch.matmul(self.Ve, torch.sigmoid(product + self.be))  # (B, T, T)
        E_normalized = F.softmax(E, dim=1)
        return E_normalized


class cheb_conv(nn.Module):
    '''
    K-order chebyshev graph convolution
    '''
    def __init__(self, K, cheb_polynomials, in_channels, out_channels):
        '''
        :param K: int
        :param in_channles: int, num of channels in the input sequence
        :param out_channels: int, num of channels in the output sequence
        '''
        super(cheb_conv, self).__init__()
        self.K = K
        self.cheb_polynomials = cheb_polynomials
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.device = cheb_polynomials[0].device
        self.Theta = nn.ParameterList([nn.Parameter(torch.FloatTensor(in_channels, out_channels).to(self.device)) for _ in range(K)])

    def forward(self, x):
        '''
        Chebyshev graph convolution operation
        :param x: (batch_size, N, F_in, T)
        :return: (batch_size, N, F_out, T)
        '''
        batch_size, num_of_vertices, in_channels, num_of_timesteps = x.shape
        outputs = []

        for time_step in range(num_of_timesteps):
            graph_signal = x[:, :, :, time_step]  # (b, N, F_in)
            output = torch.zeros(batch_size, num_of_vertices, self.out_channels).to(self.device)  # (b, N, F_out)
            for k in range(self.K):
                T_k = self.cheb_polynomials[k]  # (N,N)
                theta_k = self.Theta[k]  # (in_channel, out_channel)
                rhs = graph_signal.permute(0, 2, 1).matmul(T_k).permute(0, 2, 1)
                output = output + rhs.matmul(theta_k)
            outputs.append(output.unsqueeze(-1))
        return F.relu(torch.cat(outputs, dim=-1))


class ASTGCN_block(nn.Module):
    def __init__(self, device, in_channels, K, nb_chev_filter, nb_time_filter, time_strides, cheb_polynomials, num_of_vertices, num_of_timesteps):
        super(ASTGCN_block, self).__init__()
        self.TAt = Temporal_Attention_layer(device, in_channels, num_of_vertices, num_of_timesteps)
        self.SAt = Spatial_Attention_layer(device, in_channels, num_of_vertices, num_of_timesteps)
        self.cheb_conv_SAt = cheb_conv_withSAt(K, cheb_polynomials, in_channels, nb_chev_filter)
        self.time_conv = nn.Conv2d(nb_chev_filter, nb_time_filter, kernel_size=(1, 3), stride=(1, time_strides), padding=(0, 1))
        self.residual_conv = nn.Conv2d(in_channels, nb_time_filter, kernel_size=(1, 1), stride=(1, time_strides))
        self.ln = nn.LayerNorm(nb_time_filter)  #需要将channel放到最后一个维度上

    def forward(self, x):
        '''
        :param x: (batch_size, N, F_in, T)
        :return: (batch_size, N, nb_time_filter, T)
        '''
        batch_size, num_of_vertices, num_of_features, num_of_timesteps = x.shape
        # TAt
        temporal_At = self.TAt(x)  # (b, T, T)
        x_TAt = torch.matmul(x.reshape(batch_size, -1, num_of_timesteps), temporal_At).reshape(batch_size, num_of_vertices, num_of_features, num_of_timesteps)
        # SAt
        spatial_At = self.SAt(x_TAt)
        # cheb gcn
        spatial_gcn = self.cheb_conv_SAt(x, spatial_At)  # (b,N,F,T)
        # spatial_gcn = self.cheb_conv(x)
        # convolution along the time axis
        time_conv_output = self.time_conv(spatial_gcn.permute(0, 2, 1, 3))  # (b,N,F,T)->(b,F,N,T) 用(1,3)的卷积核去做->(b,F,N,T)
        # residual shortcut
        x_residual = self.residual_conv(x.permute(0, 2, 1, 3))  # (b,N,F,T)->(b,F,N,T) 用(1,1)的卷积核去做->(b,F,N,T)
        x_residual = self.ln(F.relu(x_residual + time_conv_output).permute(0, 3, 2, 1)).permute(0, 2, 3, 1)
        # (b,F,N,T)->(b,T,N,F) -ln-> (b,T,N,F)->(b,N,F,T)
        return x_residual


class ASTGCN(nn.Module):
    def __init__(self, device, cheb_polynomials, num_for_predict, len_input, num_of_vertices, in_channels, nb_block=2,  K=3, nb_chev_filter=64, nb_time_filter=64, time_strides=2):
        '''
        :param nb_block:
        :param in_channels:
        :param K:
        :param nb_chev_filter:
        :param nb_time_filter:
        :param time_strides:
        :param cheb_polynomials:
        :param nb_predict_step:
        '''
        super(ASTGCN, self).__init__()
        self.BlockList = nn.ModuleList([ASTGCN_block(device, in_channels, K, nb_chev_filter, nb_time_filter, time_strides, cheb_polynomials, num_of_vertices, len_input)])
        self.BlockList.extend([ASTGCN_block(device, nb_time_filter, K, nb_chev_filter, nb_time_filter, 1, cheb_polynomials, num_of_vertices, len_input//time_strides) for _ in range(nb_block-1)])
        self.final_conv = nn.Conv2d(int(len_input/time_strides), num_for_predict, kernel_size=(1, nb_time_filter))
        self.device = device
        self.to(device)

    def forward(self, x):
        '''
        :param x: (B, T, N, C)
        :param x: (B, N, C, T)
        :return: (B, T, N, C)
        '''
        x = x.permute(0, 2, 3, 1) # from (B, T, N, C) to (B, N, C, T)
        for block in self.BlockList:
            x = block(x)
        output = self.final_conv(x.permute(0, 3, 1, 2)) # (b,T,N,C)
        return output

def weight_matrix(W, sigma2=0.1, epsilon=0.5):
    '''
    :param sigma2: float, scalar of matrix W.
    :param epsilon: float, thresholds to control the sparsity of matrix W.
    :param scaling: bool, whether applies numerical scaling on W.
    :return: np.ndarray, [n_route, n_route].
    '''
    n = W.shape[0]
    W = W /10000
    W[W==0]=np.inf
    W2 = W * W
    W_mask = (np.ones([n, n]) - np.identity(n))
    return np.exp(-W2 / sigma2) * (np.exp(-W2 / sigma2) >= epsilon) * W_mask

def scaled_laplacian(A):
    n = A.shape[0]
    d = np.sum(A, axis=1)
    L = np.diag(d) - A
    for i in range(n):
        for j in range(n):
            if d[i] > 0 and d[j] > 0:
                L[i, j] /= np.sqrt(d[i] * d[j])
    lam = np.linalg.eigvals(L).max().real
    return 2 * L / lam - np.eye(n)

def cheb_poly(L, Ks):
    n = L.shape[0]
    LL = [np.eye(n), L[:]]
    for i in range(2, Ks):
        LL.append(np.matmul(2 * L, LL[-1]) - LL[-2])
    return np.asarray(LL)

def main():
    N_NODE, CHANNEL, TIMESTEP_IN, TIMESTEP_OUT = 8, 1, 12, 12
    GPU = '3'
    device = torch.device("cuda:{}".format(GPU)) if torch.cuda.is_available() else torch.device("cpu")
    adj_mx = np.eye(N_NODE)
    L = scaled_laplacian(adj_mx)
    cheb_polynomials = [torch.from_numpy(i).type(torch.FloatTensor).to(device) for i in cheb_poly(L, Ks=3)]
    model = ASTGCN(device, cheb_polynomials, num_for_predict=TIMESTEP_OUT, len_input=TIMESTEP_IN, num_of_vertices=N_NODE, in_channels=CHANNEL).to(device)
    summary(model, (TIMESTEP_IN, N_NODE, CHANNEL), device=device)
    
if __name__ == '__main__':
    main()
