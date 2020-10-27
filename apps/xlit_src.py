import torch
import torch.nn as nn
import numpy as np
import random
import sys
import os
import json
from annoy import AnnoyIndex

F_DIR = os.path.dirname(os.path.realpath(__file__))

##=================== Network ==================================================

class Encoder(nn.Module):
    def __init__(self, input_dim, embed_dim, hidden_dim ,
                       rnn_type = 'gru', layers = 1,
                       bidirectional =False,
                       dropout = 0, device = "cpu"):
        super(Encoder, self).__init__()

        self.input_dim = input_dim #src_vocab_sz
        self.enc_embed_dim = embed_dim
        self.enc_hidden_dim = hidden_dim
        self.enc_rnn_type = rnn_type
        self.enc_layers = layers
        self.enc_directions = 2 if bidirectional else 1
        self.device = device

        self.embedding = nn.Embedding(self.input_dim, self.enc_embed_dim)

        if self.enc_rnn_type == "gru":
            self.enc_rnn = nn.GRU(input_size= self.enc_embed_dim,
                          hidden_size= self.enc_hidden_dim,
                          num_layers= self.enc_layers,
                          bidirectional= bidirectional)
        elif self.enc_rnn_type == "lstm":
            self.enc_rnn = nn.LSTM(input_size= self.enc_embed_dim,
                          hidden_size= self.enc_hidden_dim,
                          num_layers= self.enc_layers,
                          bidirectional= bidirectional)
        else:
            raise Exception("unknown RNN type mentioned")

    def forward(self, x, x_sz, hidden = None):
        """
        x_sz: (batch_size, 1) -  Unpadded sequence lengths used for pack_pad
        """
        batch_sz = x.shape[0]
        # x: batch_size, max_length, enc_embed_dim
        x = self.embedding(x)

        ## pack the padded data
        # x: max_length, batch_size, enc_embed_dim -> for pack_pad
        x = x.permute(1,0,2)
        x = nn.utils.rnn.pack_padded_sequence(x, x_sz, enforce_sorted=False) # unpad

        # output: packed_size, batch_size, enc_embed_dim
        # hidden: n_layer**num_directions, batch_size, hidden_dim | if LSTM (h_n, c_n)
        output, hidden = self.enc_rnn(x) # gru returns hidden state of all timesteps as well as hidden state at last timestep

        ## pad the sequence to the max length in the batch
        # output: max_length, batch_size, enc_emb_dim*directions)
        output, _ = nn.utils.rnn.pad_packed_sequence(output)

        # output: batch_size, max_length, hidden_dim
        output = output.permute(1,0,2)

        return output, hidden

    def get_word_embedding(self, x):
        """
        """
        x_sz = torch.tensor([len(x)])
        x_ = torch.tensor(x).unsqueeze(0).to(dtype=torch.long)
        # x: 1, max_length, enc_embed_dim
        x = self.embedding(x_)

        ## pack the padded data
        # x: max_length, 1, enc_embed_dim -> for pack_pad
        x = x.permute(1,0,2)
        x = nn.utils.rnn.pack_padded_sequence(x, x_sz, enforce_sorted=False) # unpad

        # output: packed_size, 1, enc_embed_dim
        # hidden: n_layer**num_directions, 1, hidden_dim | if LSTM (h_n, c_n)
        output, hidden = self.enc_rnn(x) # gru returns hidden state of all timesteps as well as hidden state at last timestep

        out_embed = hidden[0].squeeze()

        return out_embed

class Decoder(nn.Module):
    def __init__(self, output_dim, embed_dim, hidden_dim,
                       rnn_type = 'gru', layers = 1,
                       use_attention = True,
                       enc_outstate_dim = None, # enc_directions * enc_hidden_dim
                       dropout = 0, device = "cpu"):
        super(Decoder, self).__init__()

        self.output_dim = output_dim #tgt_vocab_sz
        self.dec_hidden_dim = hidden_dim
        self.dec_embed_dim = embed_dim
        self.dec_rnn_type = rnn_type
        self.dec_layers = layers
        self.use_attention = use_attention
        self.device = device
        if self.use_attention:
            self.enc_outstate_dim = enc_outstate_dim if enc_outstate_dim else hidden_dim
        else:
            self.enc_outstate_dim = 0


        self.embedding = nn.Embedding(self.output_dim, self.dec_embed_dim)

        if self.dec_rnn_type == 'gru':
            self.dec_rnn = nn.GRU(input_size= self.dec_embed_dim + self.enc_outstate_dim, # to concat attention_output
                          hidden_size= self.dec_hidden_dim, # previous Hidden
                          num_layers= self.dec_layers,
                          batch_first = True )
        elif self.dec_rnn_type == "lstm":
            self.dec_rnn = nn.LSTM(input_size= self.dec_embed_dim + self.enc_outstate_dim, # to concat attention_output
                          hidden_size= self.dec_hidden_dim, # previous Hidden
                          num_layers= self.dec_layers,
                          batch_first = True )
        else:
            raise Exception("unknown RNN type mentioned")

        self.fc = nn.Sequential(
            nn.Linear(self.dec_hidden_dim, self.dec_embed_dim), nn.LeakyReLU(),
            # nn.Linear(self.dec_embed_dim, self.dec_embed_dim), nn.LeakyReLU(), # removing to reduce size
            nn.Linear(self.dec_embed_dim, self.output_dim),
            )

        ##----- Attention ----------
        if self.use_attention:
            self.W1 = nn.Linear( self.enc_outstate_dim, self.dec_hidden_dim)
            self.W2 = nn.Linear( self.dec_hidden_dim, self.dec_hidden_dim)
            self.V = nn.Linear( self.dec_hidden_dim, 1)

    def attention(self, x, hidden, enc_output):
        '''
        x: (batch_size, 1, dec_embed_dim) -> after Embedding
        enc_output: batch_size, max_length, enc_hidden_dim *num_directions
        hidden: n_layers, batch_size, hidden_size | if LSTM (h_n, c_n)
        '''

        ## perform addition to calculate the score

        # hidden_with_time_axis: batch_size, 1, hidden_dim
        ## hidden_with_time_axis = hidden.permute(1, 0, 2) ## replaced with below 2lines
        hidden_with_time_axis = torch.sum(hidden, axis=0) if self.dec_rnn_type != "lstm" \
                                else torch.sum(hidden[0], axis=0) # h_n

        hidden_with_time_axis = hidden_with_time_axis.unsqueeze(1)

        # score: batch_size, max_length, hidden_dim
        score = torch.tanh(self.W1(enc_output) + self.W2(hidden_with_time_axis))

        # attention_weights: batch_size, max_length, 1
        # we get 1 at the last axis because we are applying score to self.V
        attention_weights = torch.softmax(self.V(score), dim=1)

        # context_vector shape after sum == (batch_size, hidden_dim)
        context_vector = attention_weights * enc_output
        context_vector = torch.sum(context_vector, dim=1)
        # context_vector: batch_size, 1, hidden_dim
        context_vector = context_vector.unsqueeze(1)

        # attend_out (batch_size, 1, dec_embed_dim + hidden_size)
        attend_out = torch.cat((context_vector, x), -1)

        return attend_out, attention_weights

    def forward(self, x, hidden, enc_output):
        '''
        x: (batch_size, 1)
        enc_output: batch_size, max_length, dec_embed_dim
        hidden: n_layer, batch_size, hidden_size | lstm: (h_n, c_n)
        '''
        if (hidden is None) and (self.use_attention is False):
            raise Exception( "No use of a decoder with No attention and No Hidden")

        batch_sz = x.shape[0]

        if hidden is None:
            # hidden: n_layers, batch_size, hidden_dim
            hid_for_att = torch.zeros((self.dec_layers, batch_sz,
                                    self.dec_hidden_dim )).to(self.device)
        elif self.dec_rnn_type == 'lstm':
            hid_for_att = hidden[1] # c_n

        # x (batch_size, 1, dec_embed_dim) -> after embedding
        x = self.embedding(x)

        if self.use_attention:
            # x (batch_size, 1, dec_embed_dim + hidden_size) -> after attention
            # aw: (batch_size, max_length, 1)
            x, aw = self.attention( x, hidden, enc_output)
        else:
            x, aw = x, 0

        # passing the concatenated vector to the GRU
        # output: (batch_size, n_layers, hidden_size)
        # hidden: n_layers, batch_size, hidden_size | if LSTM (h_n, c_n)
        output, hidden = self.dec_rnn(x, hidden) if hidden is not None else self.dec_rnn(x)

        # output :shp: (batch_size * 1, hidden_size)
        output =  output.view(-1, output.size(2))

        # output :shp: (batch_size * 1, output_dim)
        output = self.fc(output)

        return output, hidden, aw


class Seq2Seq(nn.Module):
    def __init__(self, encoder, decoder, pass_enc2dec_hid=False, dropout = 0, device = "cpu"):
        super(Seq2Seq, self).__init__()

        self.encoder = encoder
        self.decoder = decoder
        self.device = device
        self.pass_enc2dec_hid = pass_enc2dec_hid
        _force_en2dec_hid_conv = False

        if self.pass_enc2dec_hid:
            assert decoder.dec_hidden_dim == encoder.enc_hidden_dim, "Hidden Dimension of encoder and decoder must be same, or unset `pass_enc2dec_hid`"
        if decoder.use_attention:
            assert decoder.enc_outstate_dim == encoder.enc_directions*encoder.enc_hidden_dim,"Set `enc_out_dim` correctly in decoder"
        assert self.pass_enc2dec_hid or decoder.use_attention, "No use of a decoder with No attention and No Hidden from Encoder"


        self.use_conv_4_enc2dec_hid = False
        if  (
              ( self.pass_enc2dec_hid and
                (encoder.enc_directions * encoder.enc_layers != decoder.dec_layers)
              )
              or _force_en2dec_hid_conv
            ):
            if encoder.enc_rnn_type == "lstm" or encoder.enc_rnn_type == "lstm":
                raise Exception("conv for enc2dec_hid not implemented; Change the layer numbers appropriately")

            self.use_conv_4_enc2dec_hid = True
            self.enc_hid_1ax = encoder.enc_directions * encoder.enc_layers
            self.dec_hid_1ax = decoder.dec_layers
            self.e2d_hidden_conv = nn.Conv1d(self.enc_hid_1ax, self.dec_hid_1ax, 1)

    def enc2dec_hidden(self, enc_hidden):
        """
        enc_hidden: n_layer, batch_size, hidden_dim*num_directions
        TODO: Implement the logic for LSTm bsed model
        """
        # hidden: batch_size, enc_layer*num_directions, enc_hidden_dim
        hidden = enc_hidden.permute(1,0,2).contiguous()
        # hidden: batch_size, dec_layers, dec_hidden_dim -> [N,C,Tstep]
        hidden = self.e2d_hidden_conv(hidden)

        # hidden: dec_layers, batch_size , dec_hidden_dim
        hidden_for_dec = hidden.permute(1,0,2).contiguous()

        return hidden_for_dec

    def active_beam_inference(self, src, beam_width=3, max_tgt_sz=50):
        ''' Search based decoding
        src: (sequence_len)
        '''
        def _avg_score(p_tup):
            """ Used for Sorting
            TODO: Dividing by length of sequence power alpha as hyperparam
            """
            return p_tup[0]

        import sys
        batch_size = 1
        start_tok = src[0]
        end_tok = src[-1]
        src_sz = torch.tensor([len(src)])
        src_ = src.unsqueeze(0)

        # enc_output: (batch_size, padded_seq_length, enc_hidden_dim*num_direction)
        # enc_hidden: (enc_layers*num_direction, batch_size, hidden_dim)
        enc_output, enc_hidden = self.encoder(src_, src_sz)

        if self.pass_enc2dec_hid:
        # dec_hidden: dec_layers, batch_size , dec_hidden_dim
            if self.use_conv_4_enc2dec_hid:
                init_dec_hidden = self.enc2dec_hidden(enc_hidden)
            else:
                init_dec_hidden = enc_hidden
        else:
            # dec_hidden -> Will be initialized to zeros internally
            init_dec_hidden = None

        # top_pred[][0] = Σ-log_softmax
        # top_pred[][1] = sequence torch.tensor shape: (1)
        # top_pred[][2] = dec_hidden
        top_pred_list = [ (0, start_tok.unsqueeze(0) , init_dec_hidden) ]

        for t in range(max_tgt_sz):
            cur_pred_list = []

            for p_tup in top_pred_list:
                if p_tup[1][-1] == end_tok:
                    cur_pred_list.append(p_tup)
                    continue

                # dec_hidden: dec_layers, 1, hidden_dim
                # dec_output: 1, output_dim
                dec_output, dec_hidden, _ = self.decoder( x = p_tup[1][-1].view(1,1), #dec_input: (1,1)
                                                    hidden = p_tup[2],
                                                    enc_output = enc_output, )

                ## π{prob} = Σ{log(prob)} -> to prevent diminishing
                # dec_output: (1, output_dim)
                dec_output = nn.functional.log_softmax(dec_output, dim=1)
                # pred_topk.values & pred_topk.indices: (1, beam_width)
                pred_topk = torch.topk(dec_output, k=beam_width, dim=1)

                for i in range(beam_width):
                    sig_logsmx_ = p_tup[0] + pred_topk.values[0][i]
                    # seq_tensor_ : (seq_len)
                    seq_tensor_ = torch.cat( (p_tup[1], pred_topk.indices[0][i].view(1)) )

                    cur_pred_list.append( (sig_logsmx_, seq_tensor_, dec_hidden) )

            cur_pred_list.sort(key = _avg_score, reverse =True) # Maximized order
            top_pred_list = cur_pred_list[:beam_width]

            # check if end_tok of all topk
            end_flags_ = [1 if t[1][-1] == end_tok else 0 for t in top_pred_list]
            if beam_width == sum( end_flags_ ): break

        pred_tnsr_list = [t[1] for t in top_pred_list ]

        return pred_tnsr_list

##===================== Glyph handlers =======================================

class GlyphStrawboss():
    def __init__(self, glyphs = 'en'):
        """ list of letters in a language in unicode
        lang: ISO Language code
        glyphs: json file with script information
        """
        if glyphs == 'en':
            # Smallcase alone
            self.glyphs = [chr(alpha) for alpha in range(97, 122+1)]
        else:
            glyph_data = json.load(open(glyphs))
            self.glyphs = glyph_data["glyphs"]

        self.char2idx = {}
        self.idx2char = {}
        self._create_index()

    def _create_index(self):

        self.char2idx['_'] = 0  #pad
        self.char2idx['$'] = 1  #start
        self.char2idx['#'] = 2  #end
        self.char2idx['*'] = 3  #Mask
        self.char2idx["'"] = 4  #apostrophe U+0027
        self.char2idx['%'] = 5  #unused
        self.char2idx['!'] = 6  #unused

        # letter to index mapping
        for idx, char in enumerate(self.glyphs):
            self.char2idx[char] = idx + 7 # +7 token initially

        # index to letter mapping
        for char, idx in self.char2idx.items():
            self.idx2char[idx] = char

    def size(self):
        return len(self.char2idx)


    def word2xlitvec(self, word):
        """ Converts given string of gyphs(word) to vector(numpy)
        Also adds tokens for start and end
        """
        try:
            vec = [self.char2idx['$']] #start token
            for i in list(word):
                vec.append(self.char2idx[i])
            vec.append(self.char2idx['#']) #end token

            vec = np.asarray(vec, dtype=np.int64)
            return vec

        except Exception as error:
            print("Error In word:", word, "Error Char not in Token:", error)
            sys.exit()

    def xlitvec2word(self, vector):
        """ Converts vector(numpy) to string of glyphs(word)
        """
        char_list = []
        for i in vector:
            char_list.append(self.idx2char[i])

        word = "".join(char_list).replace('$','').replace('#','') # remove tokens
        word = word.replace("_", "").replace('*','') # remove tokens
        return word


class VocabSanitizer():
    def __init__(self, data_file):
        '''
        data_file: path to file conatining vocabulary list
        '''
        extension = os.path.splitext(data_file)[-1]
        if extension == ".json":
            self.vocab_set  = set( json.load(open(data_file)) )
        elif extension == ".csv":
            self.vocab_df = pd.read_csv(data_file).set_index('WORD')
            self.vocab_set = set( self.vocab_df.index )
        else:
            print("Only Json/CSV file extension supported")

    def reposition(self, word_list):
        '''Reorder Words in list
        '''
        new_list = []
        temp_ = word_list.copy()
        for v in word_list:
            if v in self.vocab_set:
                new_list.append(v)
                temp_.remove(v)
        new_list.extend(temp_)

        return new_list


##=============== INSTANTIATION ================================================


device = "cpu"
en_glyph = GlyphStrawboss("en")
gom_glyph = GlyphStrawboss("mai")
voc_sanitize = VocabSanitizer(F_DIR+"/mai_all_words_sorted.json")

input_dim = en_glyph.size()
output_dim = gom_glyph.size()
enc_emb_dim = 300
dec_emb_dim = 300
enc_hidden_dim = 512
dec_hidden_dim = 512
rnn_type = "lstm"
enc2dec_hid = True
attention = True
enc_layers = 1
dec_layers = 2
m_dropout = 0
enc_bidirect = True
enc_outstate_dim = enc_hidden_dim * (2 if enc_bidirect else 1)

enc = Encoder(  input_dim= input_dim, embed_dim = enc_emb_dim,
                hidden_dim= enc_hidden_dim,
                rnn_type = rnn_type, layers= enc_layers,
                dropout= m_dropout, device = device,
                bidirectional= enc_bidirect)
dec = Decoder(  output_dim= output_dim, embed_dim = dec_emb_dim,
                hidden_dim= dec_hidden_dim,
                rnn_type = rnn_type, layers= dec_layers,
                dropout= m_dropout,
                use_attention = attention,
                enc_outstate_dim= enc_outstate_dim,
                device = device,)

model = Seq2Seq(enc, dec, pass_enc2dec_hid=enc2dec_hid, device=device)
model = model.to(device)
weights = torch.load( F_DIR+"/mai_prediction120.pth", map_location=torch.device(device))
model.load_state_dict(weights)
model.eval()

def character_model(word, topk = 5):
    in_vec = torch.from_numpy(en_glyph.word2xlitvec(word)).to(device)
    ## change to active or passive beam
    p_out_list = model.active_beam_inference(in_vec, beam_width = topk)
    p_result = [ gom_glyph.xlitvec2word(out.cpu().numpy()) for out in p_out_list]

    result = voc_sanitize.reposition(p_result)

    #List type
    return result

def numsym_model(seg):
    ''' native_numsym[x] returns a list object
    '''
    if len(seg) == 1:
        return native_numsym[n] + [seg]

    a = [native_numsym[n][0] for n in seg]
    return ["".join(a)] + [seg]


def word_segementer(sequence):
    num_set = set("1234567890.")
    char_set = set("abcdefghijklmnopqrstuvwxyz")
    nat_set = set().union(native_scripts, native_numsym.values())


    sequence = sequence.lower()
    accepted = set().union(num_set, char_set, nat_set)
    sequence = ''.join([i for i in sequence if i in accepted])

    segment = []
    idx = 0
    seq_ = list(sequence)
    while len(seq_):
        # for Number-Symbol
        temp = ""
        while len(seq_) and seq_[0] in num_set:
            temp += seq_[0]
            seq_.pop(0)
        if temp != "": segment.append(temp)

        # for Target Chars
        temp = ""
        while len(seq_) and seq_[0] in nat_set:
            temp += seq_[0]
            seq_.pop(0)
        if temp != "": segment.append(temp)

        # for Input-Roman Chars
        temp = ""
        while len(seq_) and seq_[0] in char_set:
            temp += seq_[0]
            seq_.pop(0)
        if temp != "": segment.append(temp)

    return segment


def inference_engine(sequence):

    num_set = set("1234567890.")
    char_set = set("abcdefghijklmnopqrstuvwxyz")
    nat_set = set().union(native_scripts, native_numsym.values())

    seg = word_segementer(sequence[:120])
    lit_seg = []

    # TODO: Fix logic to accomodate up to 5 segments rather than 1st one
    p = 0; model_flag = True
    seg_len = len(seg[:3])
    while p < seg_len:
        if seg[p][0] in nat_set:
            lit_seg.append([seg[p]])
            p+=1; continue

        if model_flag:
            if seg[p][0] in char_set:
                lit_seg.append(character_model(seg[p]))
                p+=1; model_flag = False
            else: # num & punc
                lit_seg.append(numsym_model(seg[p]))
                p+=1; model_flag = False
        else:
            p+=1


    final_result = [""]
    for seg in lit_seg:
        new_result = []
        for s in seg:
            for f in final_result:
                new_result.append(f+s)
        final_result = new_result

    return final_result