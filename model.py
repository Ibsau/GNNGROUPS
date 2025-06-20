import math
import torch
import torch.nn as nn
from torch_geometric.nn import GCNConv
from datasetEpisode import GCNDataset, collate_fn
from torch.utils.data import DataLoader
from getModel import getModel
from configReader import read_config

class BaseModel(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.to(self.device)

        self.input_dim     = int(config["model"]["input_dim"])
        self.hidden_dim    = int(config["model"]["hidden_dim"])
        self.output_dim    = int(config["model"]["output_dim"])
        self.num_timesteps = int(config["dataset"]["timesteps"])
        self.num_nodes     = int(config["dataset"]["nodes"])
        self.batch_size    = int(config["training"]["batch_size"])
        self.batches = int(config["training"]["batch_size"])
        self.max_nodes = self.batches * self.num_nodes

        self.register_buffer("pos_embed", self.get_sinusoidal_encoding(self.num_timesteps, self.hidden_dim))

    def get_sinusoidal_encoding(self, timesteps, dim):
        position = torch.arange(timesteps).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, dim, 2) * (-math.log(10000.0) / dim))
        encoding = torch.zeros(timesteps, dim)
        encoding[:, 0::2] = torch.sin(position * div_term)
        encoding[:, 1::2] = torch.cos(position * div_term)
        return encoding
    
    def tensor_to_edge_index(adj):
        if adj.dim() != 2 or adj.size(0) != adj.size(1):
            raise ValueError("The input tensor must be a square (NxN) matrix.")
        src, dst = torch.nonzero(adj, as_tuple=True)
        edge_index = torch.stack([src, dst], dim=0)
        return edge_index


# Simple Baseline Models:

class LSTMOnly(BaseModel):
    def __init__(self, config):
        super().__init__(config)

        self.lstm = nn.LSTM(self.input_dim, self.hidden_dim, batch_first=True)
        self.fc   = nn.Linear(self.hidden_dim, self.output_dim)

    def forward(self, batch, eval=False):
        # positions: [B, T, N, D], ego_mask: [B, T, N]
        positions = batch['positions'][..., :self.input_dim].to(self.device)
        ego_mask  = batch['ego_mask_batch'].to(self.device)             # [B, T, N]
        B, T, N, D = positions.shape

        # reshape to [B*N, T, D] for LSTM
        feats = positions.permute(0,2,1,3).reshape(B * N, T, D)

        # run LSTM + output projection
        lstm_out, _ = self.lstm(feats)                                  # [B*N, T, hidden_dim]
        out = self.fc(lstm_out)                                         # [B*N, T, output_dim]
        out = out.view(B, N, T, self.output_dim)                        # [B, N, T, output_dim]

        # ego_mask: [B, T, N] -> [B, N, T, 1]
        mask = ego_mask.permute(0,2,1).unsqueeze(-1).type_as(out)
        out = out * mask

        return out

class GCNOnly(BaseModel):
    def __init__(self, config):
        super().__init__(config)

        self.gcn1 = GCNConv(self.input_dim, self.hidden_dim)
        self.gcn2 = GCNConv(self.hidden_dim, self.hidden_dim)
        self.fc   = nn.Linear(self.hidden_dim, self.output_dim)

    def forward(self, batch, eval=False):
        x  = batch['big_batch_positions'].to(self.device)       # [T, B*N, D]
        A  = batch['big_batched_adjacency_pruned'].to(self.device)  # [T, B*N, B*N]
        mask = batch['ego_mask_batch'].permute(1,0,2).reshape(self.num_timesteps, -1).to(self.device)
        B = batch['ego_mask_batch'].shape[0]
        N = self.num_nodes

        outs = []
        for t in range(self.num_timesteps):
            feats_t = x[t]                                  # [B*N, D]
            m_t     = mask[t]                               # [B*N]
            idx     = m_t.nonzero(as_tuple=False).squeeze()
            feats_m = feats_t[idx]
            A_m     = A[t][idx][:, idx]
            edges   = self.tensor_to_edge_index(A_m)

            h1 = torch.relu(self.gcn1(feats_m, edges))
            h2 = torch.relu(self.gcn2(h1, edges))
            out_feats = self.fc(h2)                         # [num_active, output_dim]

            placeholder = torch.zeros(B * N, self.output_dim, device=self.device)
            placeholder[idx] = out_feats
            outs.append(placeholder.unsqueeze(0))          # [1, B*N, out_dim]

        h_stack = torch.cat(outs, dim=0)                   # [T, B*N, out_dim]
        h_stack = h_stack.view(self.num_timesteps, B, N, self.output_dim)
        return h_stack.permute(1,2,0,3)                    # [B, N, T, out_dim]

class DynamicGraphNN(BaseModel):
    def __init__(self, config):
        super().__init__(config)

        self.gcn      = GCNConv(self.input_dim, self.hidden_dim)
        self.gru_cell = nn.GRUCell(self.hidden_dim, self.hidden_dim)
        self.fc       = nn.Linear(self.hidden_dim, self.output_dim)

    def forward(self, batch, eval=False):
        x    = batch['big_batch_positions'].to(self.device)    # [T, B*N, D]
        A    = batch['big_batched_adjacency_pruned'].to(self.device)
        mask = batch['ego_mask_batch'].permute(1,0,2).reshape(self.num_timesteps, -1).to(self.device)
        B = batch['ego_mask_batch'].shape[0]
        N = self.num_nodes

        # Initialize hidden state for all nodes
        h_t = torch.zeros(B * N, self.hidden_dim, device=self.device)
        seq = []

        for t in range(self.num_timesteps):
            feats_t = x[t]                                # [B*N, D]
            m_t     = mask[t].nonzero(as_tuple=False).squeeze()
            fm      = feats_t[m_t]
            Am      = A[t][m_t][:, m_t]
            edges   = self.tensor_to_edge_index(Am)

            gcn_out   = torch.relu(self.gcn(fm, edges))  # [num_active, hidden_dim]
            h_prev    = h_t[m_t]                         # [num_active, hidden_dim]
            h_new     = self.gru_cell(gcn_out, h_prev)   # [num_active, hidden_dim]
            h_t[m_t]  = h_new                            # update only active nodes
            seq.append(h_t.unsqueeze(0))                 # [1, B*N, hidden_dim]

        seq_stack = torch.cat(seq, dim=0)              # [T, B*N, hidden_dim]
        seq_stack = seq_stack.view(self.num_timesteps, B, N, self.hidden_dim)
        out = self.fc(seq_stack)                       # [T, B, N, output_dim]
        return out.permute(1,2,0,3)                    # [B, N, T, output_dim]

# TemporalGCN

class TemporalGCN(BaseModel):
    def __init__(self, config):
        super().__init__(config)
        
        # Graph convolution layers
        self.gcn1 = GCNConv(self.input_dim, self.hidden_dim)
        self.gcn2 = GCNConv(self.hidden_dim, self.hidden_dim)
        
        # LSTM for temporal dependencies
        self.lstm = nn.LSTM(self.hidden_dim, self.hidden_dim)
        # Fully connected output layer
        self.fc1 = nn.Linear(self.hidden_dim, self.hidden_dim_2)
        self.fc2 = nn.Linear(self.hidden_dim_2, self.output_dim)
        
    def forward(self, batch, eval=False):
        ego_mask = batch['ego_mask_batch'] # Shape: (Batch, Timestep, Node Amt)
        x = batch['big_batch_positions']
        big_batch_adjacency = batch['big_batched_adjacency_pruned']

        num_timesteps = x.shape[0]
        num_nodes = x.shape[1]

        x_out = []

        # In eval mode?
        if eval:
            B = 1
            x_ego_mask = ego_mask
            max_nodes = ego_mask.size(dim=1)
        else:
            B = len(ego_mask)
            # [Batch, Time, Nodes] -> [Time, Batch*Nodes]
            x_ego_mask = ego_mask.permute(1, 0, 2).reshape(num_timesteps, -1) # Prepare ego_mask to mask x
            max_nodes = ego_mask.size(dim=2)

        x_placeholder = torch.zeros(num_timesteps, max_nodes*B, self.hidden_dim).to(self.device)

        for t in range(num_timesteps):
            x_t = x[t]                      # Get features at timestamp t
            a_t = big_batch_adjacency[t]    # Get adjacency at timestamp t
            ego_mask_t = x_ego_mask[t]      # Get ego mask at timestamp t
            
            # Post Pad:
            x_t_m = x_t[ego_mask_t]                   # Mask features
            a_t_m = a_t[ego_mask_t][:, ego_mask_t]    # Mask adjacency

            e_t = self.tensor_to_edge_index(a_t_m)         # Convert adjacency matrix to edge index (2, Y)
            
            x_t_g1 = self.gcn1(x_t_m, e_t)               # Pass masked features and adjacency
            x_t_r = torch.relu(x_t_g1)
            x_t_g2 = self.gcn2(x_t_r, e_t)

            # Post Pad
            ego_idx = torch.nonzero(ego_mask_t).flatten().to(self.device)
            x_placeholder[t, ego_idx] = x_t_g2         # Insert embeddings into their corresponding place in the global matrix (Padding)

        # Rearrange the placeholder for LSTM processing.
        # Currently: [T, B * max_nodes, hidden_dim] -> reshape to (B*num_nodes, T, hidden_dim)
        x_placeholder = x_placeholder.transpose(0, 1)  # Now shape: (B * max_nodes, T, hidden_dim)

        lstm_out, (h_n, _) = self.lstm(x_placeholder)

        # Reshape to [B, max_nodes, T, hidden_dim]
        embeddings = lstm_out.view(B, max_nodes, num_timesteps, self.hidden_dim)

        embeddings = torch.relu(self.fc1(embeddings))
        embeddings = self.fc2(embeddings)  # [B, max_nodes, T, output_dim]

        return embeddings


# AttentionGCN

class AttentionGCNOld(BaseModel):
    def __init__(self, config):
        super().__init__(config)
        
        # Graph convolution layers
        self.gcn1 = GCNConv(self.input_dim, self.hidden_dim)
        self.gcn2 = GCNConv(self.hidden_dim, self.hidden_dim)
        
        # LSTM for temporal dependencies
        self.lstm = nn.LSTM(self.hidden_dim, self.hidden_dim)

        self.multi_attention = nn.ModuleList([
            nn.MultiheadAttention(self.hidden_dim, self.num_heads, batch_first=True)
            for _ in range(self.max_nodes)
        ])
        self.query = nn.ModuleList([
            nn.Linear(self.hidden_dim, self.hidden_dim)
            for _ in range(self.max_nodes)
        ])
        self.value = nn.ModuleList([
            nn.Linear(self.hidden_dim, self.hidden_dim)
            for _ in range(self.max_nodes)
        ])
        self.key = nn.ModuleList([
            nn.Linear(self.hidden_dim, self.hidden_dim)
            for _ in range(self.max_nodes)
        ])
        # Fully connected output layer
        self.fc1 = nn.Linear(self.hidden_dim, self.hidden_dim_2)
        self.fc2 = nn.Linear(self.hidden_dim_2, self.output_dim)
        
    def forward(self, batch, eval=False):
        ego_mask = batch['ego_mask_batch'] # Shape: (Batch, Timestep, Node Amt)
        x = batch['big_batch_positions']
        big_batch_adjacency = batch['big_batched_adjacency_pruned']

        num_timesteps = x.shape[0]
        num_nodes = x.shape[1]

        x_out = []

        # In eval mode?
        if eval:
            B = 1
            x_ego_mask = ego_mask
            max_nodes = ego_mask.size(dim=1)
        else:
            B = len(ego_mask)
            # [Batch, Time, Nodes] -> [Time, Batch*Nodes]
            x_ego_mask = ego_mask.permute(1, 0, 2).reshape(num_timesteps, -1) # Prepare ego_mask to mask x
            max_nodes = ego_mask.size(dim=2)

        x_placeholder = torch.zeros(num_timesteps, max_nodes*B, self.hidden_dim).to(self.device)

        for t in range(num_timesteps):
            x_t = x[t]                      # Get features at timestamp t
            a_t = big_batch_adjacency[t]    # Get adjacency at timestamp t
            ego_mask_t = x_ego_mask[t]      # Get ego mask at timestamp t
            
            # Post Pad:
            x_t_m = x_t[ego_mask_t]                   # Mask features
            a_t_m = a_t[ego_mask_t][:, ego_mask_t]    # Mask adjacency

            e_t = self.tensor_to_edge_index(a_t_m)         # Convert adjacency matrix to edge index (2, Y)
            
            x_t_g1 = self.gcn1(x_t_m, e_t)               # Pass masked features and adjacency
            x_t_r = torch.relu(x_t_g1)
            x_t_g2 = self.gcn2(x_t_r, e_t)

            # Post Pad
            ego_idx = torch.nonzero(ego_mask_t).flatten().to(self.device)
            x_placeholder[t, ego_idx] = x_t_g2         # Insert embeddings into their corresponding place in the global matrix (Padding)

        # Rearrange the placeholder for LSTM processing.
        # Currently: [T, B * max_nodes, hidden_dim] -> reshape to (B*num_nodes, T, hidden_dim)
        x_placeholder = x_placeholder.transpose(0, 1)  # Now shape: (B * max_nodes, T, hidden_dim)

        x_placeholder = x_placeholder + self.pos_embed.unsqueeze(0)  # add positional info


        # Each node embedding gets mul by matrix to get query and key vector

        # At each timestep, the node query is dot product by every other node key
        # -> divide by sqrt of that dimension in k/q space
        # -> set forward entries to neg infinity  
        # -> softmaxed

        outputs = []
        for attn, query_l, value_l, key_l, node_emb in zip(self.multi_attention, self.query, self.value, self.key, x_placeholder):
            # Add batch dimension
            node_emb = node_emb.unsqueeze(0)  # Now shape: (1, T, hidden_dim)
            # Compute Q, K, V representations
            query = query_l(node_emb)
            key   = key_l(node_emb)
            value = value_l(node_emb)
            # Apply multi-head attention; attn_out will be (1, T, hidden_dim)
            attn_out, _ = attn(query, key, value)
            # Remove the extra batch dimension and collect the result
            outputs.append(attn_out.squeeze(0))
        # Combine all outputs: resulting shape will be (B*max_nodes, T, hidden_dim)
        x_attn = torch.stack(outputs, dim=0)

        x_out = x_attn.view(B, max_nodes, self.num_timesteps, self.hidden_dim)
        
        return x_out


class AttentionGCN(BaseModel):
    def __init__(self, config):
        super().__init__(config)

        self.gcn1 = GCNConv(self.input_dim,  self.hidden_dim)
        self.gcn2 = GCNConv(self.hidden_dim, self.hidden_dim)

        self.lstm = nn.LSTM(self.hidden_dim, self.hidden_dim, batch_first=True)

        self.multi_attention = nn.MultiheadAttention(
            embed_dim=self.hidden_dim,
            num_heads=self.num_heads,
            batch_first=True
        )

        self.query = nn.Linear(self.hidden_dim, self.hidden_dim)
        self.key   = nn.Linear(self.hidden_dim, self.hidden_dim)
        self.value = nn.Linear(self.hidden_dim, self.hidden_dim)

        self.fc1 = nn.Linear(self.hidden_dim,   self.hidden_dim_2)
        self.fc2 = nn.Linear(self.hidden_dim_2, self.output_dim)


    def forward(self, batch, eval=False):

        ego_mask             = batch['ego_mask_batch']               # [B, T, N]
        x_raw                = batch['big_batch_positions']          # [T, B*N, input_dim]
        big_batch_adjacency  = batch['big_batched_adjacency_pruned']  # [T, B*N, B*N]

        T = self.num_timesteps
        B = ego_mask.shape[0]
        N = ego_mask.shape[2]
        H = self.hidden_dim

        # [T, B*N, H]
        x_placeholder = torch.zeros(T, B * N, H, device=self.device)

        # [T, B*N]
        ego_mask_flat = ego_mask.permute(1, 0, 2).reshape(T, B * N)  # [T, B*N]

        for t in range(T):
            x_t       = x_raw[t]           # [B*N, input_dim]
            A_t       = big_batch_adjacency[t]  # [B*N, B*N]
            mask_flat = ego_mask_flat[t]   # [B*N] bool

            if not mask_flat.any():
                # no nodes active at time t
                continue

            idx      = mask_flat.nonzero(as_tuple=True)[0] 
            x_t_m    = x_t[idx]                           # [n_active, input_dim]
            A_t_m    = A_t[idx][:, idx]                   # [n_active, n_active]
            edge_idx = self.tensor_to_edge_index(A_t_m)        # [2, num_edges]

            h1 = torch.relu(self.gcn1(x_t_m, edge_idx))   # [n_active, H]
            h2 = torch.relu(self.gcn2(h1,    edge_idx))   # [n_active, H]

            x_placeholder[t, idx, :] = h2

        # Permute → [B*N, T, H] so that MultiheadAttention (with batch_first=True) sees:
        #   batch_size = B*N, seq_len = T, embed_dim = H.
        x_seq = x_placeholder.permute(1, 0, 2).contiguous()  # [B*N, T, H]

        #   pos_embed: [T, H] → unsqueeze(0) → [1, T, H], broadcast to [B*N, T, H]
        x_seq = x_seq + self.pos_embed.unsqueeze(0)  # [B*N, T, H]

        Q = self.query(x_seq)  # [B*N, T, H]
        K = self.key(x_seq)    # [B*N, T, H]
        V = self.value(x_seq)  # [B*N, T, H]

        attn_out, _ = self.multi_attention(Q, K, V)
        x_out = attn_out.view(B, N, T, H)  # [B, N, T, H]

        return x_out


if __name__ == '__main__':

    model_cfg, dataset_cfg, training_cfg = read_config("config.ini")

    dataset = GCNDataset(dataset_cfg["val_path"])


    batch_size = training_cfg["batch_size"]
    time_steps = dataset_cfg["timesteps"]

    model = getModel(eval=False)

    # Create DataLoader
    dataloader = DataLoader(dataset, batch_size=batch_size, collate_fn=collate_fn, shuffle=True)

    for batch_idx, batch in enumerate(dataloader):
        positions = batch['positions'] # batch, timestamp, node_amt, 3
        ego_mask_batch = batch['ego_mask_batch']
        big_batch_positions = batch['big_batch_positions']
        big_batch_adjacency = batch['big_batch_adjacency']
        trainOT_state = None
        for time in range(time_steps):
            emb, trainOT_state = model(batch, time, trainOT_state)

            print(emb.shape)

            exit()