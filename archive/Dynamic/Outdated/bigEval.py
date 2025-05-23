from model import TemporalGCN 
from torch.utils.data import Dataset, DataLoader
import torch
from tqdm import tqdm
from makeEpisode import getEgo
import numpy as np
import itertools
from sklearn.cluster import KMeans
from animate import plot_faster

class GCNDataset(Dataset):
    def __init__(self, data_path):
        super().__init__()
        self.data = torch.load(data_path)  # list of (positions, adjacency)
        
    def __len__(self):
        return len(self.data)
    
    def __getitem__(self, idx):
        positions, adjacency = self.data[idx]
        # positions: shape [time_steps, node_amt, 3]
        # adjacency: shape [time_steps, node_amt, node_amt]
        return positions, adjacency

def collate_fn(batch):
    return batch

def adjacency_to_edge_index(adj_t: torch.Tensor):
    # (node_i, node_j) for all 1-entries
    edge_index = adj_t.nonzero().t().contiguous()  # shape [2, E]
    return edge_index

def compute_best_accuracy(true_labels, pred_labels, n_clusters):
    """
    Given the ground truth labels and predicted labels, try all possible
    permutations of cluster label mappings to determine the highest accuracy.

    Args:
        true_labels (np.ndarray): Array of shape (n_samples,) with the true labels.
        pred_labels (np.ndarray): Array of shape (n_samples,) with the predicted cluster labels.
        n_clusters (int): Number of clusters.
        
    Returns:
        best_accuracy (float): Highest accuracy achieved with the best permutation.
        best_perm (tuple): The permutation (mapping) of predicted labels that gave the best accuracy.
    """
    best_accuracy = 0.0
    best_perm = None
    best_map = None
    # Iterate over all possible permutations of cluster indices (0,1,...,n_clusters-1)
    for perm in itertools.permutations(range(n_clusters)):
        # Create a copy of predicted labels mapped using the current permutation
        mapped_pred = np.zeros_like(pred_labels)
        for orig_label, new_label in enumerate(perm):
            mapped_pred[pred_labels == orig_label] = new_label
        # Compute accuracy as the fraction of labels that match the ground truth
        accuracy = np.mean(mapped_pred == true_labels)
        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_perm = perm
            best_map  = mapped_pred
    return best_accuracy, best_perm, best_map


def cross_entropy_clustering(embeddings, n_clusters, n_iters=100):
    """
    Cluster the data using an EM algorithm on a Gaussian mixture model,
    which effectively minimizes a cross entropy between the data and the
    cluster distributions.
    
    Args:
        embeddings (np.ndarray): Array of shape (n_samples, n_features).
        n_clusters (int): The desired number of clusters.
        n_iters (int): Number of EM iterations.
    
    Returns:
        final_labels (np.ndarray): Cluster label for each sample.
        means (np.ndarray): Final cluster means.
        covariances (np.ndarray): Final cluster covariance matrices.
        priors (np.ndarray): Final cluster priors.
    """
    n_samples, n_features = embeddings.shape

    # ---------------------------
    # Initialization via KMeans
    # ---------------------------
    kmeans = KMeans(n_clusters=n_clusters, random_state=42).fit(embeddings)
    labels = kmeans.labels_
    means = kmeans.cluster_centers_
    covariances = np.zeros((n_clusters, n_features, n_features))
    priors = np.zeros(n_clusters)
    for k in range(n_clusters):
        cluster_data = embeddings[labels == k]
        # Use np.cov with rowvar=False. Add a small regularization term to avoid singular matrices.
        covariances[k] = np.cov(cluster_data, rowvar=False) + 1e-6 * np.eye(n_features)
        priors[k] = cluster_data.shape[0] / n_samples

    # ---------------------------
    # EM iterations
    # ---------------------------
    for i in range(n_iters):
        # E-step: compute responsibilities
        responsibilities = np.zeros((n_samples, n_clusters))
        for k in range(n_clusters):
            diff = embeddings - means[k]
            inv_cov = np.linalg.inv(covariances[k])
            det_cov = np.linalg.det(covariances[k])
            # Compute the Gaussian probability density for cluster k:
            exponent = -0.5 * np.sum((diff @ inv_cov) * diff, axis=1)
            coef = priors[k] / np.sqrt(((2 * np.pi) ** n_features) * det_cov)
            responsibilities[:, k] = coef * np.exp(exponent)
        # Normalize so that each row sums to 1
        responsibilities /= responsibilities.sum(axis=1, keepdims=True)

        # M-step: update parameters using the soft assignments
        for k in range(n_clusters):
            Nk = responsibilities[:, k].sum()
            if Nk > 0:
                means[k] = (responsibilities[:, k][:, None] * embeddings).sum(axis=0) / Nk
                diff = embeddings - means[k]
                # Compute covariance: weighted outer products summed over samples
                covariances[k] = (
                    responsibilities[:, k][:, None, None]
                    * np.einsum("ni,nj->nij", diff, diff)
                ).sum(axis=0) / Nk
                # Regularize covariance to avoid numerical issues
                covariances[k] += 1e-6 * np.eye(n_features)
                priors[k] = Nk / n_samples

    # Final assignment: choose the cluster with maximum responsibility for each sample
    final_labels = responsibilities.argmax(axis=1)
    return final_labels, means, covariances, priors

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


input_dim = 2
output_dim = 16
num_nodes = 400 
num_timesteps = 20 
hidden_dim = 64 


model = TemporalGCN(
    input_dim=input_dim,
    output_dim=output_dim,
    num_nodes=num_nodes,
    num_timesteps=num_timesteps,
    hidden_dim=hidden_dim
).to(device)


checkpoint_path = "best_model.pt"
model.load_state_dict(torch.load(checkpoint_path, map_location=device))


model.eval()


val_dataset = GCNDataset('val_data.pt')

val_loader = DataLoader(val_dataset, 
                            batch_size=4, 
                            shuffle=False, 
                            collate_fn=collate_fn)
acc_all = []
for batch_idx, sample_list in enumerate(val_loader):
        batch_embeddings = []
        batch_groups = []

        for (positions, adjacency) in sample_list:
            ego_idx, ego_positions, ego_adjacency = getEgo(positions, adjacency)
            group_ids = ego_positions[0, :, 2].long()
            time_steps, node_amt, _ = ego_positions.shape

            ego_positions_no_id = ego_positions[:, :, :2]
            positions_transposed = ego_positions_no_id.permute(1, 0, 2) # shape: [node_amt, time_steps, 3]
            x = positions_transposed.unsqueeze(0).to(device)

            # Convert adjacency to list of edge_indices
            edge_indices = []
            for t in range(time_steps):
                adj_t = ego_adjacency[t]
                edge_index_t = adjacency_to_edge_index(adj_t).to(device)
                edge_indices.append(edge_index_t)
            
            emb = model(x, edge_indices)
            emb_np = emb.cpu().detach().numpy().squeeze(0)

            n_clusters = torch.unique(group_ids).size(0)
            labels, means, covs, priors = cross_entropy_clustering(emb_np, n_clusters=n_clusters, n_iters=100)

            # print("Cluster labels for each node:", labels)

            # Convert group_ids (ground truth) to a numpy array
            true_labels = group_ids.cpu().numpy()

            # 'labels' are produced from cross entropy clustering
            accuracy, best_perm, pred_groups = compute_best_accuracy(true_labels, labels, 4)

            # print("Best permutation mapping (predicted label -> true label):", best_perm)
            print("Best clustering accuracy: {:.4f}".format(accuracy))

            acc_all.append(accuracy)
            # batch_embeddings.append(emb.squeeze(0))
            # batch_groups.append(group_ids.to(device))
            
            plot_faster(positions, adjacency, embed=emb, ego_idx=ego_idx, ego_network_indices=ego_positions, pred_groups=pred_groups)
            input("Press Enter to continue...")
        
        # batch_embeddings = torch.stack(batch_embeddings, dim=0)
        # batch_groups = torch.stack(batch_groups, dim=0)

acc_avg = sum(acc_all) / len(acc_all)
print("ACC AVERAGE: ")
print(acc_avg)

# ACC AVERAGE 0.855