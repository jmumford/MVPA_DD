#!/usr/bin/env python3
"""
Delay Discounting MVPA Analysis Pipeline
A comprehensive pipeline for analyzing delay discounting fMRI data including:
1. Behavioral modeling (hyperbolic discounting)
2. MVPA decoding analysis 
3. Neural geometry analysis

Author: Cognitive Neuroscience Lab, Stanford University
"""

import os
import sys
import numpy as np
import pandas as pd
import pickle
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

# Core scientific computing
from scipy import stats, optimize
from scipy.spatial.distance import pdist, squareform
import matplotlib.pyplot as plt
import seaborn as sns

# Neuroimaging
import nibabel as nib
from nilearn import image, masking, plotting
from nilearn.regions import RegionExtractor
from nilearn.input_data import NiftiMasker

# Machine learning
from sklearn.model_selection import (
    StratifiedKFold, LeaveOneOut, permutation_test_score,
    cross_val_score, train_test_split, KFold
)
from sklearn.svm import SVC
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.preprocessing import StandardScaler, LabelEncoder
from sklearn.metrics import (
    accuracy_score, classification_report, confusion_matrix,
    roc_auc_score, mean_squared_error, r2_score
)
from sklearn.decomposition import PCA
from sklearn.manifold import MDS, TSNE, Isomap

# Statistical analysis
import statsmodels.api as sm
from statsmodels.stats.multitest import multipletests

# Parallel processing
from joblib import Parallel, delayed
import multiprocessing as mp

# Configuration
from oak_storage_config import OAKConfig as Config

# Data utilities
from data_utils import (
    load_behavioral_data, load_fmri_data, load_confounds, 
    extract_roi_timeseries, get_complete_subjects, check_mask_exists,
    load_mask, DataError, SubjectManager
)

# MVPA utilities (NEW!)
from mvpa_utils import (
    run_classification, run_regression, extract_neural_patterns,
    run_dimensionality_reduction, MVPAConfig, MVPAError,
    run_choice_classification, run_continuous_decoding,
    update_mvpa_config
)

# Add memory-efficient imports after existing imports
from memory_efficient_data import (
    MemoryEfficientLoader, MemoryConfig, create_memory_efficient_loader
)

class BehavioralAnalysis:
    """Class for behavioral analysis and discounting parameter estimation"""
    
    def __init__(self, config):
        self.config = config
        
    def hyperbolic_discount_function(self, delay, k):
        """
        Hyperbolic discounting function: V = 1 / (1 + k * delay)
        
        Parameters:
        -----------
        delay : array-like
            Delay in days
        k : float
            Discount rate parameter
            
        Returns:
        --------
        array-like : Discounted value
        """
        return 1 / (1 + k * delay)
    
    def subjective_value(self, amount, delay, k):
        """
        Calculate subjective value given amount, delay, and discount rate
        
        Parameters:
        -----------
        amount : array-like
            Monetary amount
        delay : array-like  
            Delay in days
        k : float
            Discount rate parameter
            
        Returns:
        --------
        array-like : Subjective value
        """
        return amount * self.hyperbolic_discount_function(delay, k)
    
    def fit_discount_rate(self, choices, large_amounts, delays, small_amount=20):
        """
        Fit hyperbolic discount rate to choice data using logistic regression
        
        Parameters:
        -----------
        choices : array-like
            Binary choices (1 = larger_later, 0 = smaller_sooner)
        large_amounts : array-like
            Amounts for larger_later option
        delays : array-like
            Delays for larger_later option
        small_amount : float
            Amount for smaller_sooner option (default: $20)
            
        Returns:
        --------
        dict : Fitted parameters and model statistics
        """
        # Create difference in subjective value as function of k
        def neg_log_likelihood(k):
            if k <= 0:
                return np.inf
            
            # Calculate subjective values
            sv_large = self.subjective_value(large_amounts, delays, k)
            sv_small = small_amount  # Immediate reward, no discounting
            
            # Difference in subjective value (larger_later - smaller_sooner)
            sv_diff = sv_large - sv_small
            
            # Logistic choice probability
            choice_prob = 1 / (1 + np.exp(-sv_diff))
            
            # Avoid log(0)
            choice_prob = np.clip(choice_prob, 1e-15, 1 - 1e-15)
            
            # Negative log likelihood
            nll = -np.sum(choices * np.log(choice_prob) + 
                         (1 - choices) * np.log(1 - choice_prob))
            
            return nll
        
        # Fit model
        try:
            result = optimize.minimize_scalar(
                neg_log_likelihood, 
                bounds=(1e-6, 1.0), 
                method='bounded'
            )
            
            k_fit = result.x
            nll_fit = result.fun
            
            # Calculate model statistics
            sv_large_fit = self.subjective_value(large_amounts, delays, k_fit)
            sv_small_fit = small_amount
            sv_diff_fit = sv_large_fit - sv_small_fit
            choice_prob_fit = 1 / (1 + np.exp(-sv_diff_fit))
            
            # Pseudo R-squared
            null_ll = -np.sum(choices * np.log(np.mean(choices)) + 
                             (1 - choices) * np.log(1 - np.mean(choices)))
            pseudo_r2 = 1 - nll_fit / null_ll
            
            return {
                'k': k_fit,
                'nll': nll_fit,
                'pseudo_r2': pseudo_r2,
                'choice_prob': choice_prob_fit,
                'sv_large': sv_large_fit,
                'sv_small': sv_small_fit,
                'sv_diff': sv_diff_fit,
                'sv_sum': sv_large_fit + sv_small_fit,
                'success': True
            }
            
        except Exception as e:
            print(f"Fitting failed: {e}")
            return {'success': False, 'error': str(e)}
    
    def process_subject_behavior(self, worker_id):
        """
        Process behavioral data for a single subject (REFACTORED to use data_utils)
        
        Parameters:
        -----------
        worker_id : str
            Subject identifier
            
        Returns:
        --------
        dict : Processed behavioral data and fitted parameters
        """
        try:
            # Use centralized data loading (NEW!)
            df_processed = load_behavioral_data(worker_id, self.config, 
                                               validate=True, compute_sv=True)
            
            # Quality control
            if len(df_processed) == 0:
                return {'worker_id': worker_id, 'success': False, 'error': 'Empty file'}
            
            # Get choices (already processed by data_utils)
            choices = df_processed['choice'].values
            
            # Check for minimum number of trials and choice variability
            valid_choices = choices[~np.isnan(choices)]
            if len(valid_choices) < 10 or valid_choices.var() == 0:
                return {'worker_id': worker_id, 'success': False, 
                       'error': 'Insufficient trials or no choice variability'}
            
            # Extract fit results if available, otherwise refit
            if 'sv_chosen' in df_processed.columns:
                # If subjective values computed successfully, extract discount rate
                # Re-fit to get exact parameters for this analysis
                valid_mask = ~np.isnan(choices)
                fit_results = self.fit_discount_rate(
                    choices[valid_mask],
                    df_processed['large_amount'].values[valid_mask],
                    df_processed['delay_days'].values[valid_mask]
                )
                
                if not fit_results['success']:
                    return {'worker_id': worker_id, 'success': False, 
                           'error': fit_results['error']}
                
                k_fit = fit_results['k']
                pseudo_r2 = fit_results['pseudo_r2']
            else:
                return {'worker_id': worker_id, 'success': False, 
                       'error': 'Subjective values not computed by data_utils'}
            
            # Add additional metadata
            df_processed['worker_id'] = worker_id
            df_processed['k'] = k_fit
            df_processed['choice_binary'] = choices  # For compatibility
            if 'choice_prob' not in df_processed.columns:
                df_processed['choice_prob'] = fit_results['choice_prob'][:len(df_processed)]
            
            return {
                'worker_id': worker_id,
                'success': True,
                'data': df_processed,
                'k': k_fit,
                'pseudo_r2': pseudo_r2,
                'n_trials': len(df_processed),
                'choice_rate': np.mean(valid_choices)
            }
            
        except DataError as e:
            return {'worker_id': worker_id, 'success': False, 'error': f'Data error: {str(e)}'}
        except Exception as e:
            return {'worker_id': worker_id, 'success': False, 'error': str(e)}

class fMRIDataLoader:
    """Class for loading and preparing fMRIPrep-preprocessed fMRI data"""
    
    def __init__(self, config, enable_memory_efficient=False, memory_config=None):
        self.config = config
        self.enable_memory_efficient = enable_memory_efficient
        
        # Initialize memory-efficient loader if requested
        if self.enable_memory_efficient:
            self.memory_loader = create_memory_efficient_loader(config, memory_config)
        else:
            self.memory_loader = None
        
    def load_subject_fmri(self, worker_id, force_memory_efficient=False):
        """
        Load fMRIPrep-preprocessed fMRI data for a subject
        
        Parameters:
        -----------
        worker_id : str
            Subject identifier
        force_memory_efficient : bool
            Force use of memory-efficient loading for this call
            
        Returns:
        --------
        dict : fMRI data and metadata
        """
        try:
            # Decide whether to use memory-efficient loading
            use_memory_efficient = (self.enable_memory_efficient or 
                                  force_memory_efficient) and self.memory_loader
            
            if use_memory_efficient:
                # Use memory-efficient loading
                fmri_data = self.memory_loader.load_fmri_memmap(worker_id)
                
                # Handle different return types
                if hasattr(fmri_data, 'array'):
                    # Memory-mapped array - create compatible interface
                    img = None  # We'll handle this differently
                    data_source = 'memmap'
                else:
                    # Regular array - wrap in nibabel-like interface
                    import nibabel as nib
                    img = nib.Nifti1Image(fmri_data, affine=np.eye(4))
                    data_source = 'memory'
                
                # Load confounds normally
                confounds_df = load_confounds(worker_id, self.config)
                confounds = None
                if confounds_df is not None:
                    confounds = confounds_df.values
                
                return {
                    'success': True,
                    'img': img,
                    'fmri_data': fmri_data,  # Raw data for memory-efficient processing
                    'confounds': confounds,
                    'data_source': data_source,
                    'memory_efficient': True,
                    'file_path': getattr(fmri_data, 'metadata', {}).get('source', 'unknown')
                }
            else:
                # Use standard loading (existing behavior)
                img = load_fmri_data(worker_id, self.config, smoothed=False, validate=True)
                confounds_df = load_confounds(worker_id, self.config)
                
                # Convert confounds to array if available
                confounds = None
                if confounds_df is not None:
                    confounds = confounds_df.values
                
                # Get file path for compatibility
                file_path = img.get_filename()
                
                return {
                    'success': True,
                    'img': img,
                    'fmri_data': None,  # Not used in standard loading
                    'confounds': confounds,
                    'data_source': 'standard',
                    'memory_efficient': False,
                    'file_path': file_path
                }
                
        except DataError as e:
            return {'success': False, 'error': f'Data error: {str(e)}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

class MVPAAnalysis:
    """Class for MVPA decoding analysis"""
    
    def __init__(self, config):
        self.config = config
        self.maskers = {}
        
    def create_roi_maskers(self):
        """Create NiftiMasker objects for each ROI"""
        from data_utils import check_mask_exists, load_mask
        
        print("Creating ROI maskers...")
        available_rois = []
        
        for roi_name, mask_path in self.config.ROI_MASKS.items():
            if check_mask_exists(mask_path):
                try:
                    # Validate mask before using
                    mask_img = load_mask(mask_path, validate=True)
                    
                    self.maskers[roi_name] = NiftiMasker(
                        mask_img=mask_path,
                        standardize=True,
                        detrend=True,
                        high_pass=0.01,
                        t_r=self.config.TR,
                        memory='nilearn_cache',
                        memory_level=1
                    )
                    
                    available_rois.append(roi_name)
                    print(f"✓ Created masker for {roi_name}")
                    
                except Exception as e:
                    print(f"✗ Failed to create masker for {roi_name}: {e}")
            else:
                print(f"✗ ROI mask not found: {roi_name} ({mask_path})")
        
        print(f"Created maskers for {len(available_rois)} ROIs: {', '.join(available_rois)}")
        
        # Ensure we have at least core ROIs available
        if hasattr(self.config, 'CORE_ROI_MASKS'):
            missing_core = [roi for roi in self.config.CORE_ROI_MASKS if roi not in available_rois]
            if missing_core:
                raise ValueError(f"Missing required core ROI masks: {missing_core}")
        
        return available_rois
    
    def create_whole_brain_masker(self):
        """Create whole-brain masker"""
        self.maskers['whole_brain'] = NiftiMasker(
            standardize=True,
            detrend=True,
            high_pass=0.01,
            t_r=self.config.TR,
            memory='nilearn_cache',
            memory_level=1
        )
    
    def extract_trial_data(self, img, events_df, roi_name, confounds=None):
        """
        Extract trial-wise neural data using GLM approach
        
        Uses centralized MVPA utilities for consistent implementation.
        
        Parameters:
        -----------
        img : nibabel image
            fMRI data
        events_df : DataFrame
            Trial events with onsets
        roi_name : str
            Name of ROI to extract from
        confounds : array-like, optional
            Confound regressors
            
        Returns:
        --------
        array : Trial-wise neural data (n_trials x n_voxels)
        """
        if roi_name not in self.maskers:
            raise ValueError(f"Masker for {roi_name} not found")
        
        masker = self.maskers[roi_name]
        
        # Use centralized pattern extraction
        result = extract_neural_patterns(
            img, events_df, masker,
            confounds=confounds,
            pattern_type='single_timepoint',
            tr=self.config.TR,
            hemi_lag=self.config.HEMI_LAG
        )
        
        if result['success']:
            return result['patterns']
        else:
            raise MVPAError(f"Pattern extraction failed: {result['error']}")
    
    def extract_trial_patterns(self, img, events_df, roi_name, confounds=None, 
                              pattern_type='single_timepoint', window_size=3):
        """
        Extract trial-wise neural patterns with different extraction methods
        
        Uses centralized MVPA utilities for consistent implementation.
        
        Parameters:
        -----------
        img : nibabel image
            fMRI data
        events_df : DataFrame
            Trial events with onsets
        roi_name : str
            Name of ROI to extract from
        confounds : array-like, optional
            Confound regressors
        pattern_type : str
            Type of pattern extraction:
            - 'single_timepoint': Single TR at peak (default)
            - 'average_window': Average over time window
            - 'temporal_profile': Full temporal profile
            - 'peak_detection': Automatic peak detection
        window_size : int
            Size of time window for averaging (in TRs)
            
        Returns:
        --------
        dict : Contains extracted patterns and metadata
        """
        if roi_name not in self.maskers:
            raise ValueError(f"Masker for {roi_name} not found")
        
        masker = self.maskers[roi_name]
        
        # Use centralized pattern extraction
        return extract_neural_patterns(
            img, events_df, masker,
            confounds=confounds,
            pattern_type=pattern_type,
            tr=self.config.TR,
            hemi_lag=self.config.HEMI_LAG,
            window_size=window_size
        )
    
    def decode_choices(self, X, y, roi_name):
        """
        Decode choice (smaller_sooner vs larger_later) from neural data
        
        Uses centralized MVPA utilities for consistent implementation.
        
        Parameters:
        -----------
        X : array
            Neural data (n_trials x n_voxels)
        y : array
            Choice labels (binary)
        roi_name : str
            Name of ROI
            
        Returns:
        --------
        dict : Decoding results
        """
        return run_choice_classification(
            X, y, 
            roi_name=roi_name,
            algorithm='svm',
            cv_strategy='stratified',
            n_permutations=self.config.N_PERMUTATIONS
        )
    
    def decode_continuous_variable(self, X, y, roi_name, variable_name):
        """
        Decode continuous variable (e.g., subjective value) from neural data
        
        Uses centralized MVPA utilities for consistent implementation.
        
        Parameters:
        -----------
        X : array
            Neural data (n_trials x n_voxels)
        y : array
            Continuous variable values
        roi_name : str
            Name of ROI
        variable_name : str
            Name of variable being decoded
            
        Returns:
        --------
        dict : Decoding results
        """
        return run_continuous_decoding(
            X, y,
            variable_name=variable_name,
            roi_name=roi_name,
            algorithm='ridge',
            cv_strategy='kfold',
            n_permutations=self.config.N_PERMUTATIONS
        )

    def extract_trial_data_memory_efficient(self, fmri_data, events_df, roi_name, confounds=None):
        """
        Extract trial-wise neural data from memory-mapped fMRI data
        
        Parameters:
        -----------
        fmri_data : MemoryMappedArray
            Memory-mapped fMRI data
        events_df : DataFrame
            Trial events with onsets
        roi_name : str
            Name of ROI to extract from
        confounds : array-like, optional
            Confound regressors
            
        Returns:
        --------
        array : Trial-wise neural data (n_trials x n_voxels)
        """
        if roi_name not in self.maskers:
            raise ValueError(f"Masker for {roi_name} not found")
        
        masker = self.maskers[roi_name]
        
        # Get trial onsets
        onsets = events_df['onset'].values
        tr = self.config.TR
        hemi_lag = self.config.HEMI_LAG
        
        # Convert onsets to TRs
        onset_trs = (onsets / tr + hemi_lag).astype(int)
        
        # Load and apply mask
        if hasattr(masker, 'mask_img_'):
            mask = masker.mask_img_.get_fdata().astype(bool)
        else:
            # Fit masker if not already fitted
            import nibabel as nib
            temp_img = nib.Nifti1Image(fmri_data.array[..., 0], affine=np.eye(4))
            masker.fit(temp_img)
            mask = masker.mask_img_.get_fdata().astype(bool)
        
        # Extract ROI voxels for each trial
        roi_indices = np.where(mask.flatten())[0]
        n_voxels = len(roi_indices)
        n_trials = len(onset_trs)
        
        # Memory-efficient trial extraction
        X = np.zeros((n_trials, n_voxels), dtype=fmri_data.dtype)
        
        for i, tr in enumerate(onset_trs):
            if tr < fmri_data.shape[3]:  # Check bounds
                volume = fmri_data.array[..., tr].flatten()
                X[i, :] = volume[roi_indices]
        
        # Apply standardization if the masker would do it
        if hasattr(masker, 'standardize') and masker.standardize:
            from sklearn.preprocessing import StandardScaler
            scaler = StandardScaler()
            X = scaler.fit_transform(X)
        
        return X

class GeometryAnalysis:
    """Class for neural geometry analysis"""
    
    def __init__(self, config):
        self.config = config
    
    def compute_neural_rdm(self, X):
        """
        Compute representational dissimilarity matrix
        
        Parameters:
        -----------
        X : array
            Neural data (n_trials x n_voxels)
            
        Returns:
        --------
        array : RDM (n_trials x n_trials)
        """
        # Compute pairwise distances
        distances = pdist(X, metric='correlation')
        rdm = squareform(distances)
        
        return rdm
    
    def dimensionality_reduction(self, X, method='pca', n_components=10):
        """
        Perform dimensionality reduction on neural data
        
        Uses centralized MVPA utilities for consistent implementation.
        
        Parameters:
        -----------
        X : array
            Neural data (n_trials x n_voxels)
        method : str
            Method for dimensionality reduction ('pca', 'mds', 'tsne', 'isomap')
        n_components : int
            Number of components
            
        Returns:
        --------
        array : Low-dimensional embedding (n_trials x n_components)
        """
        result = run_dimensionality_reduction(
            X, method=method, n_components=n_components
        )
        
        if result['success']:
            return result['embedding'], result['reducer']
        else:
            raise MVPAError(f"Dimensionality reduction failed: {result['error']}")
    
    def behavioral_geometry_correlation(self, embedding, behavioral_vars):
        """
        Correlate behavioral variables with neural geometry
        
        Parameters:
        -----------
        embedding : array
            Low-dimensional neural embedding (n_trials x n_components)
        behavioral_vars : dict
            Dictionary of behavioral variables (each n_trials length)
            
        Returns:
        --------
        dict : Correlation results
        """
        results = {}
        
        for var_name, var_values in behavioral_vars.items():
            correlations = []
            p_values = []
            
            for dim in range(embedding.shape[1]):
                r, p = stats.pearsonr(embedding[:, dim], var_values)
                correlations.append(r)
                p_values.append(p)
            
            results[var_name] = {
                'correlations': np.array(correlations),
                'p_values': np.array(p_values),
                'max_correlation': np.max(np.abs(correlations)),
                'best_dimension': np.argmax(np.abs(correlations))
            }
        
        return results
    
    def visualize_embeddings(self, embedding, behavioral_vars, roi_name, 
                           output_dir=None, figsize=(15, 12)):
        """
        Create comprehensive visualizations of neural embeddings
        
        Parameters:
        -----------
        embedding : array
            Low-dimensional neural embedding (n_trials x n_components)
        behavioral_vars : dict
            Dictionary of behavioral variables
        roi_name : str
            Name of ROI for plot titles
        output_dir : str, optional
            Directory to save plots
        figsize : tuple
            Figure size for plots
            
        Returns:
        --------
        dict : Dictionary of created figures
        """
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d import Axes3D
        
        if output_dir is None:
            output_dir = Path("./geometry_plots")
        else:
            output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True)
        
        figures = {}
        n_dims = embedding.shape[1]
        n_trials = embedding.shape[0]
        
        # 1. 2D scatter plots for first few dimensions
        if n_dims >= 2:
            fig, axes = plt.subplots(2, 3, figsize=figsize)
            axes = axes.ravel()
            
            # Plot different combinations of dimensions
            dim_pairs = [(0, 1), (0, 2), (1, 2), (0, 3), (1, 3), (2, 3)]
            
            for i, (dim1, dim2) in enumerate(dim_pairs[:6]):
                if i < len(axes) and dim1 < n_dims and dim2 < n_dims:
                    ax = axes[i]
                    
                    # Color by choice if available
                    if 'choice' in behavioral_vars:
                        choices = behavioral_vars['choice']
                        colors = ['red' if c == 0 else 'blue' for c in choices]
                        scatter = ax.scatter(embedding[:, dim1], embedding[:, dim2], 
                                           c=colors, alpha=0.6, s=50)
                        # Create legend manually
                        red_patch = plt.Line2D([0], [0], marker='o', color='w', 
                                              markerfacecolor='red', markersize=8, label='Smaller-Sooner')
                        blue_patch = plt.Line2D([0], [0], marker='o', color='w', 
                                               markerfacecolor='blue', markersize=8, label='Larger-Later')
                        ax.legend(handles=[red_patch, blue_patch])
                    else:
                        ax.scatter(embedding[:, dim1], embedding[:, dim2], alpha=0.6, s=50)
                    
                    ax.set_xlabel(f'Dimension {dim1 + 1}')
                    ax.set_ylabel(f'Dimension {dim2 + 1}')
                    ax.set_title(f'{roi_name}: Dims {dim1+1} vs {dim2+1}')
                    ax.grid(True, alpha=0.3)
            
            # Remove empty subplots
            for i in range(len(dim_pairs), len(axes)):
                if i < len(axes):
                    fig.delaxes(axes[i])
            
            plt.tight_layout()
            fig.suptitle(f'{roi_name} - Neural Embedding 2D Projections', 
                        fontsize=16, y=1.02)
            
            filename = output_dir / f'{roi_name}_embedding_2d.png'
            plt.savefig(filename, dpi=300, bbox_inches='tight')
            figures['2d_projections'] = filename
            plt.close()
        
        # 2. 3D visualization
        if n_dims >= 3:
            fig = plt.figure(figsize=(12, 8))
            
            # Create 2x2 subplot for different 3D views
            for i, view_angle in enumerate([(30, 45), (45, 30), (60, 60), (15, 75)]):
                ax = fig.add_subplot(2, 2, i+1, projection='3d')
                
                if 'choice' in behavioral_vars:
                    choices = behavioral_vars['choice']
                    colors = ['red' if c == 0 else 'blue' for c in choices]
                    ax.scatter(embedding[:, 0], embedding[:, 1], embedding[:, 2], 
                             c=colors, alpha=0.7, s=50)
                else:
                    ax.scatter(embedding[:, 0], embedding[:, 1], embedding[:, 2], 
                             alpha=0.7, s=50)
                
                ax.set_xlabel('Dimension 1')
                ax.set_ylabel('Dimension 2')
                ax.set_zlabel('Dimension 3')
                ax.set_title(f'View {i+1}')
                ax.view_init(elev=view_angle[0], azim=view_angle[1])
            
            fig.suptitle(f'{roi_name} - 3D Neural Embedding', fontsize=16)
            plt.tight_layout()
            
            filename = output_dir / f'{roi_name}_embedding_3d.png'
            plt.savefig(filename, dpi=300, bbox_inches='tight')
            figures['3d_embedding'] = filename
            plt.close()
        
        # 3. Behavioral variable mapping
        n_vars = len(behavioral_vars)
        if n_vars > 0:
            fig, axes = plt.subplots(2, min(3, (n_vars + 1) // 2), figsize=(15, 10))
            if n_vars == 1:
                axes = [axes]
            elif axes.ndim == 1:
                axes = axes
            else:
                axes = axes.ravel()
            
            for i, (var_name, var_values) in enumerate(behavioral_vars.items()):
                if i < len(axes):
                    ax = axes[i]
                    
                    # Use first two dimensions for 2D plot
                    if n_dims >= 2:
                        scatter = ax.scatter(embedding[:, 0], embedding[:, 1], 
                                           c=var_values, cmap='viridis', 
                                           alpha=0.7, s=50)
                        plt.colorbar(scatter, ax=ax, label=var_name)
                    else:
                        ax.scatter(range(len(var_values)), var_values, alpha=0.7)
                        ax.set_xlabel('Trial')
                        ax.set_ylabel(var_name)
                    
                    ax.set_title(f'{var_name} Mapping')
                    if n_dims >= 2:
                        ax.set_xlabel('Dimension 1')
                        ax.set_ylabel('Dimension 2')
                    ax.grid(True, alpha=0.3)
            
            # Remove empty subplots
            for i in range(n_vars, len(axes)):
                if i < len(axes):
                    fig.delaxes(axes[i])
            
            fig.suptitle(f'{roi_name} - Behavioral Variable Mapping', fontsize=16)
            plt.tight_layout()
            
            filename = output_dir / f'{roi_name}_behavioral_mapping.png'
            plt.savefig(filename, dpi=300, bbox_inches='tight')
            figures['behavioral_mapping'] = filename
            plt.close()
        
        # 4. Trajectory analysis (if temporal structure exists)
        if 'sv_diff' in behavioral_vars:
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
            
            # Sort trials by subjective value difference
            sv_diff = behavioral_vars['sv_diff']
            sort_idx = np.argsort(sv_diff)
            
            # Plot embedding trajectory sorted by sv_diff
            if n_dims >= 2:
                ax1.plot(embedding[sort_idx, 0], embedding[sort_idx, 1], 
                        'o-', alpha=0.6, markersize=4)
                ax1.set_xlabel('Dimension 1')
                ax1.set_ylabel('Dimension 2')
                ax1.set_title('Neural Trajectory by SV Difference')
                ax1.grid(True, alpha=0.3)
                
                # Add arrow to show direction
                n_points = len(sort_idx)
                for i in range(0, n_points-1, max(1, n_points//10)):
                    if i+1 < n_points:
                        dx = embedding[sort_idx[i+1], 0] - embedding[sort_idx[i], 0]
                        dy = embedding[sort_idx[i+1], 1] - embedding[sort_idx[i], 1]
                        ax1.arrow(embedding[sort_idx[i], 0], embedding[sort_idx[i], 1], 
                                 dx*0.8, dy*0.8, head_width=0.02, head_length=0.02, 
                                 fc='red', ec='red', alpha=0.7)
            
            # Plot distance from origin as function of behavioral variable
            distances = np.sqrt(np.sum(embedding**2, axis=1))
            ax2.scatter(sv_diff, distances, alpha=0.6)
            
            # Add trend line
            z = np.polyfit(sv_diff, distances, 1)
            p = np.poly1d(z)
            ax2.plot(sv_diff, p(sv_diff), "r--", alpha=0.8)
            
            # Calculate correlation
            r, p_val = stats.pearsonr(sv_diff, distances)
            ax2.set_xlabel('Subjective Value Difference')
            ax2.set_ylabel('Distance from Origin')
            ax2.set_title(f'Neural Distance vs Behavior (r={r:.3f}, p={p_val:.3f})')
            ax2.grid(True, alpha=0.3)
            
            fig.suptitle(f'{roi_name} - Neural Trajectory Analysis', fontsize=16)
            plt.tight_layout()
            
            filename = output_dir / f'{roi_name}_trajectory_analysis.png'
            plt.savefig(filename, dpi=300, bbox_inches='tight')
            figures['trajectory_analysis'] = filename
            plt.close()
        
        print(f"Embedding visualizations for {roi_name} saved to {output_dir}")
        return figures
    
    def compare_embeddings_by_condition(self, embedding, condition_labels, 
                                      condition_names=None, n_permutations=1000):
        """
        Formally compare neural geometries between conditions
        
        Parameters:
        -----------
        embedding : array
            Low-dimensional neural embedding (n_trials x n_components)
        condition_labels : array
            Condition labels for each trial (e.g., 0=smaller_sooner, 1=larger_later)
        condition_names : list, optional
            Names for conditions (default: ['Condition 0', 'Condition 1'])
        n_permutations : int
            Number of permutations for statistical testing
            
        Returns:
        --------
        dict : Comprehensive comparison results
        """
        from scipy.spatial.distance import pdist, squareform
        from scipy.stats import permutationtest
        
        if condition_names is None:
            condition_names = [f'Condition {i}' for i in np.unique(condition_labels)]
        
        unique_conditions = np.unique(condition_labels)
        n_conditions = len(unique_conditions)
        
        if n_conditions < 2:
            raise ValueError("Need at least 2 conditions for comparison")
        
        results = {
            'condition_names': condition_names,
            'n_conditions': n_conditions,
            'n_trials_per_condition': [np.sum(condition_labels == c) for c in unique_conditions]
        }
        
        # 1. Representational Similarity Analysis (RSA)
        print("Computing representational similarity analysis...")
        
        # Compute RDMs for each condition
        rdms = {}
        for i, cond in enumerate(unique_conditions):
            cond_mask = condition_labels == cond
            cond_embedding = embedding[cond_mask]
            
            # Compute representational dissimilarity matrix
            distances = pdist(cond_embedding, metric='euclidean')
            rdm = squareform(distances)
            rdms[condition_names[i]] = rdm
        
        # Compare RDMs between conditions
        if n_conditions == 2:
            rdm1 = rdms[condition_names[0]]
            rdm2 = rdms[condition_names[1]]
            
            # Flatten upper triangular parts
            triu_indices = np.triu_indices(rdm1.shape[0], k=1)
            rdm1_flat = rdm1[triu_indices]
            triu_indices = np.triu_indices(rdm2.shape[0], k=1)
            rdm2_flat = rdm2[triu_indices]
            
            # Correlate RDMs (representational similarity)
            rsa_correlation, rsa_p = stats.pearsonr(rdm1_flat, rdm2_flat)
            
            results['rsa'] = {
                'correlation': rsa_correlation,
                'p_value': rsa_p,
                'interpretation': 'High correlation = similar geometries'
            }
        
        # 2. Procrustes Analysis
        print("Computing Procrustes analysis...")
        
        if n_conditions == 2:
            cond1_mask = condition_labels == unique_conditions[0]
            cond2_mask = condition_labels == unique_conditions[1]
            
            cond1_embedding = embedding[cond1_mask]
            cond2_embedding = embedding[cond2_mask]
            
            # Procrustes analysis (requires equal number of points)
            min_trials = min(len(cond1_embedding), len(cond2_embedding))
            
            if min_trials > 5:  # Need sufficient points
                # Randomly sample equal numbers
                np.random.seed(42)
                cond1_sample = cond1_embedding[np.random.choice(len(cond1_embedding), min_trials, replace=False)]
                cond2_sample = cond2_embedding[np.random.choice(len(cond2_embedding), min_trials, replace=False)]
                
                # Procrustes transformation
                from scipy.spatial.distance import procrustes
                mtx1, mtx2, disparity = procrustes(cond1_sample, cond2_sample)
                
                results['procrustes'] = {
                    'disparity': disparity,
                    'interpretation': 'Low disparity = similar shapes after optimal alignment'
                }
        
        # 3. Geometric Property Comparisons
        print("Computing geometric property comparisons...")
        
        geometric_props = {}
        
        for i, cond in enumerate(unique_conditions):
            cond_mask = condition_labels == cond
            cond_embedding = embedding[cond_mask]
            
            # Compute geometric properties
            centroid = np.mean(cond_embedding, axis=0)
            
            # Variance (spread)
            variance = np.var(cond_embedding, axis=0)
            total_variance = np.sum(variance)
            
            # Distance from origin
            distances_from_origin = np.sqrt(np.sum(cond_embedding**2, axis=1))
            mean_distance_from_origin = np.mean(distances_from_origin)
            
            # Inter-point distances (compactness)
            pairwise_distances = pdist(cond_embedding, metric='euclidean')
            mean_pairwise_distance = np.mean(pairwise_distances)
            
            # Convex hull volume (if sufficient points and dimensions)
            convex_hull_volume = None
            if len(cond_embedding) > embedding.shape[1] and embedding.shape[1] <= 3:
                try:
                    from scipy.spatial import ConvexHull
                    hull = ConvexHull(cond_embedding)
                    convex_hull_volume = hull.volume
                except:
                    pass
            
            geometric_props[condition_names[i]] = {
                'centroid': centroid,
                'total_variance': total_variance,
                'mean_distance_from_origin': mean_distance_from_origin,
                'mean_pairwise_distance': mean_pairwise_distance,
                'convex_hull_volume': convex_hull_volume
            }
        
        results['geometric_properties'] = geometric_props
        
        # 4. Statistical Tests on Geometric Properties
        print("Running permutation tests...")
        
        if n_conditions == 2:
            def permutation_test_geometric_property(prop_name):
                """Run permutation test for a geometric property"""
                
                def compute_difference(labels):
                    """Compute difference in property between conditions"""
                    props_temp = {}
                    for i, cond in enumerate(unique_conditions):
                        cond_mask = labels == cond
                        cond_embedding = embedding[cond_mask]
                        
                        if prop_name == 'total_variance':
                            props_temp[i] = np.sum(np.var(cond_embedding, axis=0))
                        elif prop_name == 'mean_distance_from_origin':
                            distances = np.sqrt(np.sum(cond_embedding**2, axis=1))
                            props_temp[i] = np.mean(distances)
                        elif prop_name == 'mean_pairwise_distance':
                            pairwise_dist = pdist(cond_embedding, metric='euclidean')
                            props_temp[i] = np.mean(pairwise_dist)
                    
                    return props_temp[1] - props_temp[0]
                
                # Observed difference
                observed_diff = compute_difference(condition_labels)
                
                # Permutation distribution
                permuted_diffs = []
                for _ in range(n_permutations):
                    permuted_labels = np.random.permutation(condition_labels)
                    permuted_diff = compute_difference(permuted_labels)
                    permuted_diffs.append(permuted_diff)
                
                permuted_diffs = np.array(permuted_diffs)
                
                # P-value (two-tailed)
                p_value = np.mean(np.abs(permuted_diffs) >= np.abs(observed_diff))
                
                return {
                    'observed_difference': observed_diff,
                    'p_value': p_value,
                    'permutation_distribution': permuted_diffs
                }
            
            # Test multiple geometric properties
            properties_to_test = ['total_variance', 'mean_distance_from_origin', 'mean_pairwise_distance']
            
            statistical_tests = {}
            for prop in properties_to_test:
                statistical_tests[prop] = permutation_test_geometric_property(prop)
            
            results['statistical_tests'] = statistical_tests
        
        # 5. Distance-based Analysis
        print("Computing distance-based analyses...")
        
        # Within vs between condition distances
        within_condition_distances = []
        between_condition_distances = []
        
        for i in range(len(embedding)):
            for j in range(i+1, len(embedding)):
                dist = np.linalg.norm(embedding[i] - embedding[j])
                
                if condition_labels[i] == condition_labels[j]:
                    within_condition_distances.append(dist)
                else:
                    between_condition_distances.append(dist)
        
        within_condition_distances = np.array(within_condition_distances)
        between_condition_distances = np.array(between_condition_distances)
        
        # Statistical test
        if len(within_condition_distances) > 0 and len(between_condition_distances) > 0:
            from scipy.stats import mannwhitneyu
            separation_statistic, separation_p = mannwhitneyu(
                within_condition_distances, between_condition_distances, 
                alternative='less'  # Test if within < between (good separation)
            )
            
            results['distance_analysis'] = {
                'mean_within_condition_distance': np.mean(within_condition_distances),
                'mean_between_condition_distance': np.mean(between_condition_distances),
                'separation_ratio': np.mean(between_condition_distances) / np.mean(within_condition_distances),
                'separation_statistic': separation_statistic,
                'separation_p_value': separation_p,
                'interpretation': 'Separation ratio > 1 indicates good condition separation'
            }
        
        # 6. Dimensionality Analysis
        print("Computing dimensionality analysis...")
        
        dimensionality_analysis = {}
        
        for i, cond in enumerate(unique_conditions):
            cond_mask = condition_labels == cond
            cond_embedding = embedding[cond_mask]
            
            # Effective dimensionality using PCA
            if len(cond_embedding) > embedding.shape[1]:
                pca = PCA()
                pca.fit(cond_embedding)
                
                # Compute participation ratio (effective dimensionality)
                eigenvalues = pca.explained_variance_
                participation_ratio = (np.sum(eigenvalues)**2) / np.sum(eigenvalues**2)
                
                # Dimensionality at 80% and 90% variance
                cumvar = np.cumsum(pca.explained_variance_ratio_)
                dim_80 = np.argmax(cumvar >= 0.8) + 1
                dim_90 = np.argmax(cumvar >= 0.9) + 1
                
                dimensionality_analysis[condition_names[i]] = {
                    'participation_ratio': participation_ratio,
                    'intrinsic_dimensionality_80': dim_80,
                    'intrinsic_dimensionality_90': dim_90,
                    'explained_variance_ratio': pca.explained_variance_ratio_
                }
        
        results['dimensionality_analysis'] = dimensionality_analysis
        
        return results
    
    def plot_geometry_comparison(self, comparison_results, embedding, condition_labels, 
                               roi_name, output_dir=None):
        """
        Create visualizations for geometry comparison results
        
        Parameters:
        -----------
        comparison_results : dict
            Results from compare_embeddings_by_condition
        embedding : array
            Low-dimensional neural embedding
        condition_labels : array
            Condition labels for each trial
        roi_name : str
            Name of ROI for plot titles
        output_dir : str, optional
            Directory to save plots
            
        Returns:
        --------
        dict : Dictionary of created figure files
        """
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d import Axes3D
        
        if output_dir is None:
            output_dir = Path("./geometry_comparison_plots")
        else:
            output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True)
        
        figures = {}
        condition_names = comparison_results['condition_names']
        unique_conditions = np.unique(condition_labels)
        
        # 1. Overlay plot showing both conditions
        if embedding.shape[1] >= 2:
            fig, axes = plt.subplots(1, 3, figsize=(18, 6))
            
            # 2D plot
            colors = ['red', 'blue', 'green', 'orange']
            for i, cond in enumerate(unique_conditions):
                cond_mask = condition_labels == cond
                axes[0].scatter(embedding[cond_mask, 0], embedding[cond_mask, 1], 
                              c=colors[i % len(colors)], alpha=0.6, s=50, 
                              label=condition_names[i])
            
            axes[0].set_xlabel('Dimension 1')
            axes[0].set_ylabel('Dimension 2')
            axes[0].set_title(f'{roi_name}: Condition Comparison')
            axes[0].legend()
            axes[0].grid(True, alpha=0.3)
            
            # Distance distributions
            if 'distance_analysis' in comparison_results:
                dist_analysis = comparison_results['distance_analysis']
                
                # Placeholder data for within/between distances visualization
                axes[1].hist([0, 1], bins=20, alpha=0.7, label=['Within Condition', 'Between Condition'])
                axes[1].set_xlabel('Distance')
                axes[1].set_ylabel('Count')
                axes[1].set_title('Distance Distributions')
                axes[1].legend()
                
                # Separation ratio
                sep_ratio = dist_analysis['separation_ratio']
                axes[1].axvline(sep_ratio, color='red', linestyle='--', 
                              label=f'Separation Ratio: {sep_ratio:.2f}')
            
            # Statistical test results
            if 'statistical_tests' in comparison_results:
                test_results = comparison_results['statistical_tests']
                
                properties = list(test_results.keys())
                p_values = [test_results[prop]['p_value'] for prop in properties]
                observed_diffs = [test_results[prop]['observed_difference'] for prop in properties]
                
                bars = axes[2].bar(range(len(properties)), [-np.log10(p) for p in p_values], 
                                 alpha=0.7)
                axes[2].axhline(-np.log10(0.05), color='red', linestyle='--', 
                              label='p = 0.05')
                axes[2].set_xlabel('Geometric Property')
                axes[2].set_ylabel('-log10(p-value)')
                axes[2].set_title('Statistical Significance')
                axes[2].set_xticks(range(len(properties)))
                axes[2].set_xticklabels([p.replace('_', ' ').title() for p in properties], 
                                      rotation=45)
                axes[2].legend()
                
                # Color bars by significance
                for i, (bar, p_val) in enumerate(zip(bars, p_values)):
                    if p_val < 0.001:
                        bar.set_color('darkgreen')
                    elif p_val < 0.01:
                        bar.set_color('green')
                    elif p_val < 0.05:
                        bar.set_color('lightgreen')
                    else:
                        bar.set_color('gray')
            
            plt.tight_layout()
            filename = output_dir / f'{roi_name}_geometry_comparison.png'
            plt.savefig(filename, dpi=300, bbox_inches='tight')
            figures['comparison_overview'] = filename
            plt.close()
        
        # 2. Dimensionality comparison
        if 'dimensionality_analysis' in comparison_results:
            dim_analysis = comparison_results['dimensionality_analysis']
            
            fig, axes = plt.subplots(1, 2, figsize=(12, 5))
            
            # Participation ratios
            conditions = list(dim_analysis.keys())
            participation_ratios = [dim_analysis[cond]['participation_ratio'] for cond in conditions]
            
            axes[0].bar(conditions, participation_ratios, alpha=0.7)
            axes[0].set_ylabel('Participation Ratio')
            axes[0].set_title('Effective Dimensionality')
            axes[0].tick_params(axis='x', rotation=45)
            
            # Intrinsic dimensionality
            dim_80 = [dim_analysis[cond]['intrinsic_dimensionality_80'] for cond in conditions]
            dim_90 = [dim_analysis[cond]['intrinsic_dimensionality_90'] for cond in conditions]
            
            x = np.arange(len(conditions))
            width = 0.35
            
            axes[1].bar(x - width/2, dim_80, width, label='80% Variance', alpha=0.7)
            axes[1].bar(x + width/2, dim_90, width, label='90% Variance', alpha=0.7)
            axes[1].set_ylabel('Number of Dimensions')
            axes[1].set_title('Intrinsic Dimensionality')
            axes[1].set_xticks(x)
            axes[1].set_xticklabels(conditions, rotation=45)
            axes[1].legend()
            
            plt.tight_layout()
            filename = output_dir / f'{roi_name}_dimensionality_comparison.png'
            plt.savefig(filename, dpi=300, bbox_inches='tight')
            figures['dimensionality_comparison'] = filename
            plt.close()
        
        print(f"Geometry comparison plots for {roi_name} saved to {output_dir}")
        return figures

def setup_directories(config):
    """Create output directories"""
    for directory in [config.OUTPUT_DIR, config.BEHAVIOR_OUTPUT, 
                     config.MVPA_OUTPUT, config.GEOMETRY_OUTPUT]:
        Path(directory).mkdir(parents=True, exist_ok=True)

def main(enable_memory_efficient=False, memory_config=None):
    """Main analysis pipeline with optional memory efficiency"""
    print("Starting Delay Discounting MVPA Analysis Pipeline")
    print("=" * 60)
    
    # Initialize configuration
    config = Config()
    setup_directories(config)
    
    # Log memory efficiency status
    if enable_memory_efficient:
        print("🚀 Memory-efficient data loading: ENABLED")
        if memory_config is None:
            memory_config = MemoryConfig()
            print(f"   Memory threshold: {memory_config.MEMMAP_THRESHOLD_GB} GB")
    else:
        print("📊 Standard data loading: ENABLED")
    
    # Initialize analysis classes
    behavioral_analysis = BehavioralAnalysis(config)
    fmri_loader = fMRIDataLoader(config, enable_memory_efficient, memory_config)
    mvpa_analysis = MVPAAnalysis(config)
    geometry_analysis = GeometryAnalysis(config)
    
    # Configure MVPA utilities to match main config
    update_mvpa_config(
        cv_folds=config.CV_FOLDS,
        n_permutations=config.N_PERMUTATIONS,
        n_jobs=1  # Conservative for memory management
    )
    
    # Create maskers
    mvpa_analysis.create_roi_maskers()
    mvpa_analysis.create_whole_brain_masker()
    
    print(f"Analysis will be performed on {len(mvpa_analysis.maskers)} ROIs")
    
    # Get list of subjects
    subjects = get_subject_list(config)
    print(f"Found {len(subjects)} subjects")
    
    # Process each subject
    all_results = {}
    
    for i, worker_id in enumerate(subjects):
        print(f"\nProcessing subject {worker_id} ({i+1}/{len(subjects)})")
        
        # 1. Process behavioral data
        print("  - Processing behavioral data...")
        behavior_result = behavioral_analysis.process_subject_behavior(worker_id)
        
        if not behavior_result['success']:
            print(f"    Failed: {behavior_result['error']}")
            continue
        
        # 2. Load fMRI data
        print("  - Loading fMRI data...")
        fmri_result = fmri_loader.load_subject_fmri(worker_id)
        
        if not fmri_result['success']:
            print(f"    Failed: {fmri_result['error']}")
            continue
        
        # Log memory efficiency status
        if fmri_result['memory_efficient']:
            print(f"    ✓ Using memory-efficient loading ({fmri_result['data_source']})")
        
        # 3. MVPA analysis
        print("  - Running MVPA analysis...")
        mvpa_results = {}
        
        behavioral_data = behavior_result['data']
        
        # Handle different data sources
        if fmri_result['memory_efficient'] and fmri_result['data_source'] == 'memmap':
            # Memory-mapped data - use special extraction
            img = None
            fmri_data = fmri_result['fmri_data']
        else:
            # Standard data
            img = fmri_result['img']
            fmri_data = None
        
        confounds = fmri_result['confounds']
        
        for roi_name in mvpa_analysis.maskers.keys():
            print(f"    - Analyzing {roi_name}...")
            
            # Extract trial-wise data
            try:
                if fmri_data is not None:
                    # Memory-efficient extraction
                    X = mvpa_analysis.extract_trial_data_memory_efficient(
                        fmri_data, behavioral_data, roi_name, confounds
                    )
                else:
                    # Standard extraction
                    X = mvpa_analysis.extract_trial_data(
                        img, behavioral_data, roi_name, confounds
                    )
                
                # Decode choices
                choice_result = mvpa_analysis.decode_choices(
                    X, behavioral_data['choice_binary'].values, roi_name
                )
                
                # Decode continuous variables
                continuous_results = {}
                for var_name in ['sv_diff', 'sv_sum', 'sv_chosen', 'sv_unchosen', 'svchosen_unchosen', 'later_delay']:
                    cont_result = mvpa_analysis.decode_continuous_variable(
                        X, behavioral_data[var_name].values, roi_name, var_name
                    )
                    continuous_results[var_name] = cont_result
                
                mvpa_results[roi_name] = {
                    'choice_decoding': choice_result,
                    'continuous_decoding': continuous_results
                }
                
            except Exception as e:
                print(f"      Error in {roi_name}: {e}")
                mvpa_results[roi_name] = {'error': str(e)}
        
        # 4. Geometry analysis
        print("  - Running geometry analysis...")
        geometry_results = {}
        
        for roi_name in mvpa_analysis.maskers.keys():
            if roi_name in mvpa_results and 'error' not in mvpa_results[roi_name]:
                try:
                    # Extract neural patterns for geometry analysis
                    X = mvpa_analysis.extract_trial_data(img, behavioral_data, roi_name, confounds)
                    
                    # Alternative: Use advanced pattern extraction for geometry
                    # pattern_results = mvpa_analysis.extract_trial_patterns(
                    #     img, behavioral_data, roi_name, confounds, 
                    #     pattern_type='average_window', window_size=5
                    # )
                    # X = pattern_results['patterns']
                    
                    # Dimensionality reduction
                    embedding, reducer = geometry_analysis.dimensionality_reduction(X, method='pca')
                    
                    # Behavioral correlations
                    behavioral_vars = {
                        'sv_diff': behavioral_data['sv_diff'].values,
                        'sv_sum': behavioral_data['sv_sum'].values,
                        'sv_chosen': behavioral_data['sv_chosen'].values,
                        'sv_unchosen': behavioral_data['sv_unchosen'].values,
                        'svchosen_unchosen': behavioral_data['svchosen_unchosen'].values,
                        'later_delay': behavioral_data['later_delay'].values,
                        'choice': behavioral_data['choice_binary'].values
                    }
                    
                    correlations = geometry_analysis.behavioral_geometry_correlation(
                        embedding, behavioral_vars
                    )
                    
                    # Create embedding visualizations
                    geometry_analysis.last_reducer = reducer  # Store for visualization
                    embedding_plots = geometry_analysis.visualize_embeddings(
                        embedding, behavioral_vars, roi_name, 
                        output_dir=f"{config.GEOMETRY_OUTPUT}/{worker_id}"
                    )
                    
                    # Formal geometry comparison between conditions
                    print(f"    - Running geometry comparisons for {roi_name}...")
                    
                    # Compare by choice (SS vs LL)
                    choice_comparison = geometry_analysis.compare_embeddings_by_condition(
                        embedding=embedding,
                        condition_labels=behavioral_data['choice_binary'].values,
                        condition_names=['Smaller Sooner', 'Larger Later'],
                        n_permutations=500  # Reduced for speed in main pipeline
                    )
                    
                    choice_comparison_plots = geometry_analysis.plot_geometry_comparison(
                        comparison_results=choice_comparison,
                        embedding=embedding,
                        condition_labels=behavioral_data['choice_binary'].values,
                        roi_name=f"{roi_name}_choice",
                        output_dir=f"{config.GEOMETRY_OUTPUT}/{worker_id}/comparisons"
                    )
                    
                    # Compare by subjective value (median split)
                    sv_median = np.median(behavioral_data['sv_diff'])
                    sv_labels = (behavioral_data['sv_diff'] > sv_median).astype(int)
                    sv_comparison = geometry_analysis.compare_embeddings_by_condition(
                        embedding=embedding,
                        condition_labels=sv_labels,
                        condition_names=['Low SV Difference', 'High SV Difference'],
                        n_permutations=500
                    )
                    
                    sv_comparison_plots = geometry_analysis.plot_geometry_comparison(
                        comparison_results=sv_comparison,
                        embedding=embedding,
                        condition_labels=sv_labels,
                        roi_name=f"{roi_name}_sv_diff",
                        output_dir=f"{config.GEOMETRY_OUTPUT}/{worker_id}/comparisons"
                    )
                    
                    # Compare by delay length (shorter vs longer delays)
                    delay_median = np.median(behavioral_data['later_delay'])
                    delay_labels = (behavioral_data['later_delay'] > delay_median).astype(int)
                    delay_comparison = geometry_analysis.compare_embeddings_by_condition(
                        embedding=embedding,
                        condition_labels=delay_labels,
                        condition_names=['Shorter Delays', 'Longer Delays'],
                        n_permutations=500
                    )
                    
                    delay_comparison_plots = geometry_analysis.plot_geometry_comparison(
                        comparison_results=delay_comparison,
                        embedding=embedding,
                        condition_labels=delay_labels,
                        roi_name=f"{roi_name}_delay_length",
                        output_dir=f"{config.GEOMETRY_OUTPUT}/{worker_id}/comparisons"
                    )
                    
                    # Additional delay-based comparisons
                    delay_specific_comparisons = {}
                    
                    # Compare immediate (0-day) vs delayed trials
                    if 'later_delay' in behavioral_data.columns:
                        immediate_labels = (behavioral_data['later_delay'] == 0).astype(int)
                        if np.sum(immediate_labels) > 5 and np.sum(1-immediate_labels) > 5:  # Ensure sufficient trials
                            immediate_comparison = geometry_analysis.compare_embeddings_by_condition(
                                embedding=embedding,
                                condition_labels=immediate_labels,
                                condition_names=['Delayed Trials', 'Immediate Trials'],
                                n_permutations=500
                            )
                            delay_specific_comparisons['immediate_vs_delayed'] = immediate_comparison
                    
                    # Compare short (≤7 days) vs long (>30 days) delays if available
                    unique_delays = sorted(behavioral_data['later_delay'].unique())
                    if len(unique_delays) > 2:
                        short_delay_mask = behavioral_data['later_delay'] <= 7
                        long_delay_mask = behavioral_data['later_delay'] > 30
                        
                        if np.sum(short_delay_mask) > 5 and np.sum(long_delay_mask) > 5:
                            # Create labels: 0=short, 1=long, exclude medium delays
                            short_long_labels = np.full(len(behavioral_data), -1)  # -1 = exclude
                            short_long_labels[short_delay_mask] = 0
                            short_long_labels[long_delay_mask] = 1
                            
                            # Only include short and long delay trials
                            include_mask = short_long_labels != -1
                            if np.sum(include_mask) > 10:
                                short_long_embedding = embedding[include_mask]
                                short_long_labels_filtered = short_long_labels[include_mask]
                                
                                short_long_comparison = geometry_analysis.compare_embeddings_by_condition(
                                    embedding=short_long_embedding,
                                    condition_labels=short_long_labels_filtered,
                                    condition_names=['Short Delays (≤7d)', 'Long Delays (>30d)'],
                                    n_permutations=500
                                )
                                delay_specific_comparisons['short_vs_long_delays'] = short_long_comparison
                    
                    # Compare by unchosen option value (median split)
                    unchosen_median = np.median(behavioral_data['sv_unchosen'])
                    unchosen_labels = (behavioral_data['sv_unchosen'] > unchosen_median).astype(int)
                    unchosen_comparison = geometry_analysis.compare_embeddings_by_condition(
                        embedding=embedding,
                        condition_labels=unchosen_labels,
                        condition_names=['Low Unchosen Value', 'High Unchosen Value'],
                        n_permutations=500
                    )
                    
                    unchosen_comparison_plots = geometry_analysis.plot_geometry_comparison(
                        comparison_results=unchosen_comparison,
                        embedding=embedding,
                        condition_labels=unchosen_labels,
                        roi_name=f"{roi_name}_unchosen_value",
                        output_dir=f"{config.GEOMETRY_OUTPUT}/{worker_id}/comparisons"
                    )
                    
                    delay_specific_comparisons['unchosen_value_comparison'] = unchosen_comparison
                    
                    # Compare chosen vs unchosen option representations
                    # This requires reshaping data to have separate embeddings for chosen and unchosen options
                    
                    # Method 1: Compare trials where chosen value > unchosen vs chosen value < unchosen
                    chosen_higher_mask = behavioral_data['sv_chosen'] > behavioral_data['sv_unchosen']
                    chosen_higher_labels = chosen_higher_mask.astype(int)
                    
                    if np.sum(chosen_higher_labels) > 5 and np.sum(1-chosen_higher_labels) > 5:
                        chosen_higher_comparison = geometry_analysis.compare_embeddings_by_condition(
                            embedding=embedding,
                            condition_labels=chosen_higher_labels,
                            condition_names=['Chosen < Unchosen Value', 'Chosen > Unchosen Value'],
                            n_permutations=500
                        )
                        
                        chosen_higher_plots = geometry_analysis.plot_geometry_comparison(
                            comparison_results=chosen_higher_comparison,
                            embedding=embedding,
                            condition_labels=chosen_higher_labels,
                            roi_name=f"{roi_name}_chosen_vs_unchosen_value",
                            output_dir=f"{config.GEOMETRY_OUTPUT}/{worker_id}/comparisons"
                        )
                        
                        delay_specific_comparisons['chosen_vs_unchosen_value'] = chosen_higher_comparison
                        delay_specific_comparisons['chosen_vs_unchosen_plots'] = chosen_higher_plots
                    
                    # Method 2: Create explicit chosen vs unchosen option comparison
                    # This analyzes whether neural patterns differ when representing chosen vs unchosen options
                    
                    try:
                        # Create doubled dataset: each trial appears twice (once as chosen, once as unchosen)
                        n_trials = len(behavioral_data)
                        
                        # Stack embeddings (trial 1 chosen, trial 1 unchosen, trial 2 chosen, ...)
                        doubled_embedding = np.tile(embedding, (2, 1))
                        
                        # Create labels: 0 = representing chosen option, 1 = representing unchosen option
                        option_type_labels = np.array([0] * n_trials + [1] * n_trials)
                        
                        # Weight by option values to see if representation strength differs
                        chosen_values = behavioral_data['sv_chosen'].values
                        unchosen_values = behavioral_data['sv_unchosen'].values
                        
                        # Scale embeddings by option values (optional analysis)
                        # doubled_embedding[:n_trials] *= chosen_values[:, np.newaxis]  # Chosen trials
                        # doubled_embedding[n_trials:] *= unchosen_values[:, np.newaxis]  # Unchosen trials
                        
                        if len(doubled_embedding) > 20:  # Ensure sufficient data
                            chosen_unchosen_comparison = geometry_analysis.compare_embeddings_by_condition(
                                embedding=doubled_embedding,
                                condition_labels=option_type_labels,
                                condition_names=['Chosen Option Representation', 'Unchosen Option Representation'],
                                n_permutations=500
                            )
                            
                            chosen_unchosen_plots = geometry_analysis.plot_geometry_comparison(
                                comparison_results=chosen_unchosen_comparison,
                                embedding=doubled_embedding,
                                condition_labels=option_type_labels,
                                roi_name=f"{roi_name}_option_type",
                                output_dir=f"{config.GEOMETRY_OUTPUT}/{worker_id}/comparisons"
                            )
                            
                            delay_specific_comparisons['chosen_unchosen_option_type'] = chosen_unchosen_comparison
                            delay_specific_comparisons['chosen_unchosen_option_plots'] = chosen_unchosen_plots
                            
                    except Exception as e:
                        print(f"        Warning: Could not perform chosen/unchosen option comparison: {e}")
                    
                    geometry_results[roi_name] = {
                        'embedding': embedding,
                        'correlations': correlations,
                        'explained_variance': reducer.explained_variance_ratio_ if hasattr(reducer, 'explained_variance_ratio_') else None,
                        'visualization_files': embedding_plots,
                        'choice_comparison': choice_comparison,
                        'sv_comparison': sv_comparison,
                        'delay_comparison': delay_comparison,
                        'delay_specific_comparisons': delay_specific_comparisons,
                        'comparison_plots': {
                            'choice': choice_comparison_plots,
                            'sv_difference': sv_comparison_plots,
                            'delay_length': delay_comparison_plots,
                            'unchosen_value': unchosen_comparison_plots
                        }
                    }
                    
                except Exception as e:
                    print(f"      Error in geometry analysis for {roi_name}: {e}")
                    geometry_results[roi_name] = {'error': str(e)}
        
        # Store results
        all_results[worker_id] = {
            'behavioral': behavior_result,
            'mvpa': mvpa_results,
            'geometry': geometry_results
        }
        
        print(f"    Completed subject {worker_id}")
    
    # Save results
    print("\nSaving results...")
    results_file = f"{config.OUTPUT_DIR}/all_results.pkl"
    with open(results_file, 'wb') as f:
        pickle.dump(all_results, f)
    
    print(f"Results saved to {results_file}")
    print("Analysis complete!")

def get_subject_list(config):
    """Get list of available subjects"""
    try:
        from data_utils import get_complete_subjects
        subjects = get_complete_subjects(config)
        return subjects
    except Exception as e:
        print(f"Warning: Could not get subject list: {e}")
        print("Using fallback method...")
        
        # Fallback: scan directories
        import os
        from pathlib import Path
        
        subjects = []
        fmri_dir = Path(config.FMRIPREP_DIR)
        
        if fmri_dir.exists():
            for subject_dir in fmri_dir.iterdir():
                if subject_dir.is_dir() and subject_dir.name.startswith('sub-'):
                    subjects.append(subject_dir.name)
        
        return sorted(subjects)

if __name__ == "__main__":
    main() 