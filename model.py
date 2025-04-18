import torch
import torch.nn as nn
from datasetEpisode import GCNDataset, collate_fn
from torch_geometric.nn import GCNConv
from torch.utils.data import DataLoader
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence, pad_sequence
import configparser

def tensor_to_edge_index(adj):
    """
    Convert an NxN adjacency matrix (PyTorch tensor) to an edge index tensor of shape [2, num_edges].

    Parameters:
        adj (torch.Tensor): A square (NxN) tensor representing the adjacency matrix.
        
    Returns:
        torch.Tensor: A tensor of shape [2, num_edges] where the first row contains the source nodes
                      and the second row contains the target nodes of the edges.
    """
    # Ensure the tensor is square
    if adj.dim() != 2 or adj.size(0) != adj.size(1):
        raise ValueError("The input tensor must be a square (NxN) matrix.")

    # Find the indices of nonzero entries (which represent edges)
    # as_tuple=True returns separate coordinate tensors for rows and columns.
    src, dst = torch.nonzero(adj, as_tuple=True)
    
    # Stack them to form an edge_index tensor of shape [2, num_edges]
    edge_index = torch.stack([src, dst], dim=0)
    return edge_index

class TemporalGCN(nn.Module):
    def __init__(self, config):
        super(TemporalGCN, self).__init__()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f"IS CUDA AVALAIBLE: {torch.cuda.is_available()}")
        self.num_nodes = int(config["dataset"]["nodes"])
        self.num_timesteps = int(config["dataset"]["timesteps"])

        self.input_dim = int(config["model"]["input_dim"])
        self.hidden_dim = int(config["model"]["hidden_dim"])
        self.hidden_dim_2 = int(config["model"]["hidden_dim_2"])
        self.output_dim = int(config["model"]["output_dim"])
        self.num_heads = int(config["model"]["num_heads"])

        self.batches = int(config["training"]["batch_size"])
        self.max_nodes = self.batches * self.num_nodes
        
        # Graph convolution layers
        self.gcn1 = GCNConv(self.input_dim, self.hidden_dim)
        self.gcn2 = GCNConv(self.hidden_dim, self.hidden_dim)
        
        # LSTM for temporal dependencies
        self.lstm = nn.LSTM(self.hidden_dim, self.output_dim)

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
        self.fc = nn.Linear(self.hidden_dim, self.output_dim)
        
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

            e_t = tensor_to_edge_index(a_t_m)         # Convert adjacency matrix to edge index (2, Y)
            
            x_t_g1 = self.gcn1(x_t_m, e_t)               # Pass masked features and adjacency
            x_t_r = torch.relu(x_t_g1)
            x_t_g2 = self.gcn2(x_t_r, e_t)

            # Post Pad
            ego_idx = torch.nonzero(ego_mask_t).flatten().to(self.device)
            x_placeholder[t, ego_idx] = x_t_g2         # Insert embeddings into their corresponding place in the global matrix (Padding)

        # Rearrange the placeholder for LSTM processing.
        # Currently: [T, B * max_nodes, hidden_dim] -> reshape to (B*num_nodes, T, hidden_dim)
        x_placeholder = x_placeholder.transpose(0, 1)  # Now shape: (B * max_nodes, T, hidden_dim)

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

        # lstm_out, (h_n, _) = self.lstm(x_placeholder)
        
        # attn_scores = self.attention(lstm_out)  # (B*num_nodes, T, 1)
        # attn_scores = attn_scores.squeeze(-1)   # (B*num_nodes, T)


        # # Compute attention weights.
        # attn_weights = F.softmax(attn_scores, dim=1)  # (B*num_nodes, T)

        # # Compute the context vector as a weighted sum of LSTM outputs (over the time dimension).
        # # Expand attn_weights and multiply elementwise with lstm_out.
        # attn_weights = attn_weights.unsqueeze(-1)    # (B*num_nodes, T, 1)
        # x_out = torch.sum(attn_weights * lstm_out, dim=1)  # (B*num_nodes, hidden_dim)
        
        # # Reshape the context vector to (B, max_nodes, hidden_dim)
        # x_out = x_out.view(B, max_nodes, self.output_dim)


        # return x_out
    
        

if __name__ == '__main__':
    config = configparser.ConfigParser()
    config.read('config.ini')

    dataset = GCNDataset(str(config["dataset"]["dataset_val"]))

    batch_size = int(config["training"]["batch_size"])

    # Define the model
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    model = TemporalGCN(config).to(device)

    # Create DataLoader
    dataloader = DataLoader(dataset, batch_size=batch_size, collate_fn=collate_fn, shuffle=True)

    for batch_idx, batch in enumerate(dataloader):
        positions = batch['positions'] # batch, timestamp, node_amt, 3
        ego_mask_batch = batch['ego_mask_batch']
        big_batch_positions = batch['big_batch_positions']
        big_batch_adjacency = batch['big_batch_adjacency']

        emb = model(batch)

        print(emb.shape)

        break
