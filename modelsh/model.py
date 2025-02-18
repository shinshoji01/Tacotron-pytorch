import torch
from torch import nn
from math import sqrt
# from utils.config import cfg
from torch.autograd import Variable
from torch.nn import functional as F
# from models.layers import Prenet, BatchNormConv1dStack

import sys
sys.path.append("/work/Git/Tacotronpytorch")
from tacotron2.utils import to_gpu, get_mask_from_lengths
from modelsh.layers import Prenet, BatchNormConv1dStack
# from utils.util import mode #, get_mask_from_lengths
from attention import get_mask_from_lengths, AttentionWrapper, attention_mechanism
from attention.gmm import GMMAttention
from attention.attention_base import LocationSensitiveAttention

def mode(obj, model = False):
	if model and cfg.is_cuda:
		obj = obj.cuda()
	elif cfg.is_cuda:
		obj = obj.cuda(non_blocking = cfg.pin_mem)
	return obj

class Postnet(nn.Module):
    """Postnet
        - A stack of five 1-d convolution layer
        - Each layer is comprised of 512 filters with shape 5*1 with Batch Normalization (BN),
          followd by tanh activations on all but the final layer
    """
    def __init__(self, mel_dim, num_convs=5, conv_channels=512, conv_kernel_size=5, conv_dropout=0.5):
        super(Postnet, self).__init__()

        activations = [torch.tanh] * (num_convs - 1) + [None]
        conv_channels = [conv_channels] * (num_convs - 1) + [mel_dim]
        self.conv1ds = BatchNormConv1dStack(mel_dim, conv_channels, kernel_size=conv_kernel_size,
                                            stride=1, padding=(conv_kernel_size -1) // 2,
                                            activations=activations, dropout=conv_dropout)

    def forward(self, x):
        # transpose to (B, mel_dim, T) for convolution,
        # and then back
        return self.conv1ds(x.transpose(1, 2)).transpose(1, 2)


class Encoder(nn.Module):
    """Encoder module:
        - A stack of three 1-d convolution layers, containing 512 filters with shape 5*1,
          followd by Batch Normalization (BN) and ReLU activations
        - Bidirectional LSTM
    """
    def __init__(self, embed_dim,
                 num_convs=3, conv_channels=512, conv_kernel_size=5,
                 conv_dropout=0.5, blstm_units=512):
        super(Encoder, self).__init__()

        # convolution layers followed by batch normalization and ReLU activation
        activations = [nn.ReLU()] * num_convs
        conv_out_channels = [conv_channels] * num_convs
        self.conv1ds = BatchNormConv1dStack(embed_dim, conv_out_channels, kernel_size=conv_kernel_size,
                                            stride=1, padding=(conv_kernel_size -1) // 2,
                                            activations=activations, dropout=conv_dropout)

        # 1 layer Bi-directional LSTM
        self.lstm = nn.LSTM(conv_channels, blstm_units // 2, 1, batch_first=True, bidirectional=True)

    def forward(self, x):
        # transpose to (B, embed_dim, T) for convolution,
        # and then back
        x = self.conv1ds(x.transpose(1, 2)).transpose(1, 2)

        # (B, T, conv_channels)
        # TODO: pack_padded, and pad_packed?
        self.lstm.flatten_parameters()
        outputs, _ = self.lstm(x)

        return outputs


class Decoder_GMM(nn.Module):
    def __init__(self, mel_dim, r, encoder_output_dim,
                 prenet_dims=[256, 256], prenet_dropout=0.5,
                 attention_dim=128, attention_rnn_units=1024, attention_dropout=0.1,
                 decoder_rnn_units=1024, decoder_rnn_layers=2, decoder_dropout=0.1,
                 max_decoder_steps=1000, stop_threshold=0.5):
        super(Decoder_GMM, self).__init__()

        self.mel_dim = mel_dim
        self.r = r
        self.attention_context_dim = attention_context_dim = encoder_output_dim
        self.attention_rnn_units = attention_rnn_units
        self.decoder_rnn_units = decoder_rnn_units
        self.max_decoder_steps = max_decoder_steps
        self.stop_threshold = stop_threshold

        # Prenet
        self.prenet = Prenet(mel_dim, prenet_dims, prenet_dropout)

        # Attention RNN
        # (prenet_out + attention context) = attention_rnn_in -> attention_rnn_out

        self.attention_rnn = AttentionWrapper(
            nn.LSTMCell(prenet_dims[-1] + attention_context_dim, attention_rnn_units),
            #attention_mechanism(cfg.attention_type)(attention_rnn_units, attention_dim)
            #GMMAttention(attention_rnn_units, attention_dim)
            LocationSensitiveAttention(attention_rnn_units, attention_dim)
        )
        self.attention_dropout = nn.Dropout(attention_dropout)
        # Process encoder_output as attention key
        self.memory_layer = nn.Linear(encoder_output_dim, attention_dim, bias=False)

        # Decoder RNN
        # (attention_rnn_out + attention context) = decoder_rnn_in -> decoder_rnn_out
        self.decoder_rnn = nn.LSTMCell(attention_rnn_units + attention_context_dim, decoder_rnn_units)
        self.decoder_dropout = nn.Dropout(decoder_dropout)

        # Project to mel
        self.mel_proj = nn.Linear(decoder_rnn_units + attention_context_dim, mel_dim * self.r)

        # Stop token prediction
        self.stop_proj = nn.Linear(decoder_rnn_units + attention_context_dim, 1)

    def forward(self, encoder_outputs, inputs=None, memory_lengths=None):
        """
        Decoder forward step.

        If decoder inputs are not given (e.g., at testing time), greedy decoding is adapted.

        Args:
            encoder_outputs: Encoder outputs. (B, T_encoder, dim)
            inputs: Decoder inputs (i.e., mel-spectrogram).
                    If None (at eval-time), previous decoder outputs are used as decoder inputs.
            memory_lengths: Encoder output (memory) lengths. If not None, used for attention masking.

        Returns:
            mel_outputs: mel outputs from the decoder.
            stop_tokens: stop token outputs from the decoder.
            attn_scores: sequence of attention weights from the decoder.
        """
        B = encoder_outputs.size(0)

        # Get processed memory for attention key
        #   - no need to call for every time step
        processed_memory = self.memory_layer(encoder_outputs)
        if memory_lengths is not None:
            mask = get_mask_from_lengths(processed_memory, memory_lengths)
        else:
            mask = None

        # Run greedy decoding if inputs is None
        greedy = inputs is None

        # Time first: (B, T, mel_dim) -> (T, B, mel_dim)
        if inputs is not None:
            inputs = inputs.transpose(0, 1)
            T_decoder = inputs.size(0)

        # <GO> frames
        initial_input = encoder_outputs.data.new(B, self.mel_dim).zero_()

        # Init decoder states
        self.attention_rnn.attention_mechanism.init_attention(processed_memory)
        attention_rnn_hidden = encoder_outputs.data.new(B, self.attention_rnn_units).zero_()
        attention_rnn_cell = encoder_outputs.data.new(B, self.attention_rnn_units).zero_()
        decoder_rnn_hidden = encoder_outputs.data.new(B, self.decoder_rnn_units).zero_()
        decoder_rnn_cell = encoder_outputs.data.new(B, self.decoder_rnn_units).zero_()
        attention_context = encoder_outputs.data.new(B, self.attention_context_dim).zero_()

        # To store the result
        mel_outputs, attn_scores, stop_tokens = [], [], []

        # Run the decoder loop
        t = 0
        current_input = initial_input
        while True:
            if t > 0:
                current_input = mel_outputs[-1][:, -1, :] if greedy else inputs[t - 1]
            t += self.r

            # Prenet
            current_input = self.prenet(current_input)

            # Attention LSTM
            (attention_rnn_hidden, attention_rnn_cell), attention_context, attention_score = self.attention_rnn(
                current_input, attention_context, (attention_rnn_hidden, attention_rnn_cell),
                encoder_outputs, processed_memory=processed_memory, mask=mask)
            attention_rnn_hidden = self.attention_dropout(attention_rnn_hidden)

            # Concat RNN output and attention context vector
            decoder_input = torch.cat((attention_rnn_hidden, attention_context), -1)

            # Pass through the decoder LSTM
            decoder_rnn_hidden, decoder_rnn_cell = self.decoder_rnn(decoder_input, (decoder_rnn_hidden, decoder_rnn_cell))
            decoder_rnn_hidden = self.decoder_dropout(decoder_rnn_hidden)

            # Contact RNN output and context vector to form projection input
            proj_input = torch.cat((decoder_rnn_hidden, attention_context), -1)

            # Project to mel
            # (B, mel_dim*r) -> (B, r, mel_dim)
            output = self.mel_proj(proj_input)
            output = output.view(B, -1, self.mel_dim)

            # Stop token prediction
            stop = self.stop_proj(proj_input)
            stop = torch.sigmoid(stop)

            # Store predictions
            mel_outputs.append(output)
            attn_scores.append(attention_score.unsqueeze(1))
            stop_tokens.extend([stop] * self.r)

            if greedy:
                if stop.min() > self.stop_threshold:
                    break
                elif t > self.max_decoder_steps:
                    print("Warning: Reached max decoder steps.")
                    break
            else:
                if t >= T_decoder:
                    break

        # To tensor
        mel_outputs = torch.cat(mel_outputs, dim=1) # (B, T_decoder, mel_dim)
        attn_scores = torch.cat(attn_scores, dim=1) # (B, T_decoder/r, T_encoder)
        stop_tokens = torch.cat(stop_tokens, dim=1) # (B, T_decoder)

        # Validation check
        assert greedy or mel_outputs.size(1) == T_decoder

        return mel_outputs, stop_tokens, attn_scores


class Tacotron2(nn.Module):
    def __init__(self, n_vocab, embed_dim=512, mel_dim=80,
                 max_decoder_steps=1000, stop_threshold=0.5, r=3):
        super(Tacotron2, self).__init__()

        self.mel_dim = mel_dim

        # Embedding
        self.embedding = nn.Embedding(n_vocab, embed_dim)
        std = sqrt(2.0 / (n_vocab + embed_dim))
        val = sqrt(3.0) * std  # uniform bounds for std
        self.embedding.weight.data.uniform_(-val, val)

        # Encoder
        self.encoder = Encoder(embed_dim)

        encoder_out_dim = cfg.encoder_embedding_dim
        ref_encoder = self.get_ref_model()
        if cfg.ref_type == 'GST':
            encoder_out_dim += cfg.gst_token_embed_dim
            self.gst = ref_encoder(mel_dim, num_tokens=cfg.gst_num_tokens,
            token_embed_dim=cfg.gst_token_embed_dim, num_heads=cfg.gst_num_heads)
        elif cfg.ref_type == 'VAE':
            encoder_out_dim += cfg.vae_latent_dim
            self.vae = ref_encoder(mel_dim)
        # Decoder
        self.decoder = Decoder(mel_dim, r, encoder_out_dim,
            max_decoder_steps=max_decoder_steps, stop_threshold=stop_threshold)
        

        # Postnet
        self.postnet = Postnet(mel_dim)
    def get_ref_model(self):
        if cfg.ref_type == 'GST':
            from model.gst import GST as ref_encoder
        elif cfg.ref_type =='VAE':
            from model.vae import VAE as ref_encoder
        else:
            return None
        return ref_encoder
    def parse_data_batch(self, batch):
        """Parse data batch to form inputs and targets for model training/evaluating
        """
        # use same device as parameters
        device = next(self.parameters()).device
        text, text_length, mel, stop, mel_length,indexs, rmel, spks = batch
        text = text.to(device).long()
        text_length = text_length.to(device).long()
        mel = mel.to(device).float()
        rmel = rmel.to(device).float()
        if mel.shape[-1] != 80:
            mel = mel.transpose(2,1)
        stop = stop.to(device).float()

        return (text, text_length, mel,rmel,mel_length), (mel, stop), indexs

    def forward(self, inputs):
        inputs, input_lengths, mels, rmel,mel_length = inputs

        B = inputs.size(0)

        # (B, T)
        inputs = self.embedding(inputs)

        # (B, T, embed_dim)
        encoder_outputs = self.encoder(inputs)

        if cfg.ref_type == 'GST':
            style_embeddings = self.gst(mels,mel_length)
        elif cfg.ref_type == 'VAE':
            style_embeddings, (mu, log_var) = self.vae(mels,mel_length)

        if cfg.ref_type != '': 
            style_embeddings = style_embeddings.repeat(1, encoder_outputs.size(1), 1)
            # (B, T, encoder_out_dim + token_embed_dim)
            encoder_outputs = torch.cat((encoder_outputs, style_embeddings), dim=2)
        
        # (B, T, mel_dim)
        mel_outputs, stop_tokens, alignments = self.decoder(
            encoder_outputs, mels, memory_lengths=input_lengths)

        # Postnet processing
        mel_post = self.postnet(mel_outputs)
        mel_post = mel_outputs + mel_post
        if cfg.ref_type == 'VAE':
            return mel_outputs, mel_post, stop_tokens, alignments, mu, log_var
        else:
            return mel_outputs, mel_post, stop_tokens, alignments

    def inference(self, inputs, rmel=None):
        if not cfg.groundtruth_alignment:
            # Only text inputs
            if not len(inputs) == 5:
                inputs = inputs, None, None, rmel, None
            else:
                inputs = inputs[0], None, None, inputs[3], None
        return self.forward(inputs)


class Tacotron2Loss(nn.Module):
    def __init__(self):
        super(Tacotron2Loss, self).__init__()
        self.kl_lambda = cfg.kl_lambda
    def KL_loss(self, mu, var):
        return torch.mean(0.5 * torch.sum(torch.exp(var) + mu ** 2 - 1. - var, 1))

    def update_lambda(self,iteration):
        iteration += 1
        if iteration % cfg.kl_step == 0:
            self.kl_lambda = self.kl_lambda + cfg.kl_incr
        if iteration <= cfg.kl_max_step and iteration % cfg.kl_step == 0:
            kl_lambda = self.kl_lambda
        elif iteration > cfg.kl_max_step and iteration % cfg.kl_step_after == 0:
            kl_lambda = self.kl_lambda
        else:
            kl_lambda = 0.0
        return min(1.0, kl_lambda)


    def forward(self, predicts, targets, iteration):
        mel_target, stop_target = targets
        mel_target.requires_grad = False
        stop_target.requires_grad = False
        if cfg.ref_type == 'VAE':
            mel_predict, mel_post_predict, stop_predict, _, mu, log_var = predicts
        else:
            mel_predict, mel_post_predict, stop_predict, _ = predicts

        mel_loss = nn.MSELoss()(mel_predict, mel_target)
        post_loss = nn.MSELoss()(mel_post_predict, mel_target)
        stop_loss = nn.BCELoss()(stop_predict, stop_target)
        if cfg.ref_type == 'VAE':
            kl_lambda = self.update_lambda(iteration)
            kl_loss = self.KL_loss(mu,log_var)
            return mel_loss + post_loss + stop_loss + kl_lambda * kl_loss
        else:
            return mel_loss + post_loss + stop_loss
