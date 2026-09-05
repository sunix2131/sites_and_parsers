import { useState, useEffect, useCallback } from 'react';
import { api } from '../api/client';
import {
  FolderOpen, Plus, Trash2, Edit, Upload, X, Image,
  ChevronRight, Link, Eye, ArrowLeft, GripVertical
} from 'lucide-react';

interface Category {
  id: number;
  name: string;
  slug: string;
  description: string;
  sort_order: number;
  is_active: boolean;
  projects_count: number;
}

interface Screenshot {
  id: number;
  project_id: number;
  filename: string;
  original_filename: string;
  url: string;
  sort_order: number;
}

interface Project {
  id: number;
  category_id: number;
  name: string;
  slug: string;
  description: string;
  client_name: string;
  screenshots: Screenshot[];
}

type View = 'categories' | 'projects' | 'screenshots';

export default function PortfolioAdmin() {
  const [categories, setCategories] = useState<Category[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedCategory, setSelectedCategory] = useState<Category | null>(null);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [view, setView] = useState<View>('categories');
  const [loading, setLoading] = useState(false);

  const [showCategoryForm, setShowCategoryForm] = useState(false);
  const [editingCategory, setEditingCategory] = useState<Category | null>(null);
  const [categoryForm, setCategoryForm] = useState({ name: '', description: '', sort_order: 0, is_active: true });

  const [showProjectForm, setShowProjectForm] = useState(false);
  const [editingProject, setEditingProject] = useState<Project | null>(null);
  const [projectForm, setProjectForm] = useState({ name: '', description: '', client_name: '' });

  const [uploading, setUploading] = useState(false);
  const [previewImage, setPreviewImage] = useState<string | null>(null);

  const loadCategories = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.get<Category[]>('/portfolio/categories');
      setCategories(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadProjects = useCallback(async (categoryId: number) => {
    setLoading(true);
    try {
      const cat = categories.find(c => c.id === categoryId);
      if (cat) {
        const data = await api.get<Project[]>(`/portfolio/categories/${cat.slug}/projects`);
        setProjects(data);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, [categories]);

  useEffect(() => {
    loadCategories();
  }, [loadCategories]);

  const saveCategory = async () => {
    if (!categoryForm.name.trim()) return;
    try {
      if (editingCategory) {
        await api.put(`/portfolio/categories/${editingCategory.id}`, categoryForm);
      } else {
        await api.post('/portfolio/categories', categoryForm);
      }
      setCategoryForm({ name: '', description: '', sort_order: 0, is_active: true });
      setShowCategoryForm(false);
      setEditingCategory(null);
      await loadCategories();
    } catch (err: any) {
      alert(err.message);
    }
  };

  const deleteCategory = async (id: number) => {
    if (!confirm('Удалить категорию и все проекты в ней?')) return;
    try {
      await api.delete(`/portfolio/categories/${id}`);
      await loadCategories();
    } catch (err: any) {
      alert(err.message);
    }
  };

  const openProjects = (category: Category) => {
    setSelectedCategory(category);
    setView('projects');
    loadProjects(category.id);
  };

  const saveProject = async () => {
    if (!projectForm.name.trim() || !selectedCategory) return;
    try {
      if (editingProject) {
        await api.put(`/portfolio/projects/${editingProject.id}`, projectForm);
      } else {
        await api.post('/portfolio/projects', { ...projectForm, category_id: selectedCategory.id });
      }
      setProjectForm({ name: '', description: '', client_name: '' });
      setShowProjectForm(false);
      setEditingProject(null);
      await loadProjects(selectedCategory.id);
    } catch (err: any) {
      alert(err.message);
    }
  };

  const deleteProject = async (id: number) => {
    if (!confirm('Удалить проект и все скриншоты?')) return;
    try {
      await api.delete(`/portfolio/projects/${id}`);
      if (selectedCategory) await loadProjects(selectedCategory.id);
    } catch (err: any) {
      alert(err.message);
    }
  };

  const openScreenshots = (project: Project) => {
    setSelectedProject(project);
    setView('screenshots');
  };

  const uploadScreenshots = async (files: FileList) => {
    if (!selectedProject || files.length === 0) return;
    setUploading(true);
    try {
      const formData = new FormData();
      Array.from(files).forEach(file => formData.append('files', file));

      const token = localStorage.getItem('token');
      const response = await fetch(`/api/portfolio/projects/${selectedProject.id}/screenshots`, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}` },
        body: formData,
      });

      if (!response.ok) throw new Error('Ошибка загрузки');

      const updated = await api.get<Project[]>(`/portfolio/categories/${selectedCategory!.slug}/projects`);
      const proj = updated.find(p => p.id === selectedProject.id);
      if (proj) setSelectedProject(proj);
    } catch (err: any) {
      alert(err.message);
    } finally {
      setUploading(false);
    }
  };

  const deleteScreenshot = async (id: number) => {
    if (!confirm('Удалить скриншот?')) return;
    try {
      await api.delete(`/portfolio/screenshots/${id}`);
      if (selectedProject) {
        setSelectedProject({
          ...selectedProject,
          screenshots: selectedProject.screenshots.filter(s => s.id !== id),
        });
      }
    } catch (err: any) {
      alert(err.message);
    }
  };

  const copyProjectLink = (project: Project) => {
    const url = `${window.location.origin}/portfolio/${selectedCategory?.slug}/${project.slug}`;
    navigator.clipboard.writeText(url);
    alert('Ссылка скопирована: ' + url);
  };

  const copyCategoryLink = (category: Category) => {
    const url = `${window.location.origin}/portfolio/${category.slug}`;
    navigator.clipboard.writeText(url);
    alert('Ссылка скопирована: ' + url);
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3 mb-4">
        {view !== 'categories' && (
          <button
            onClick={() => {
              if (view === 'screenshots') {
                setView('projects');
                setSelectedProject(null);
              } else {
                setView('categories');
                setSelectedCategory(null);
                setProjects([]);
              }
            }}
            className="p-2 rounded-lg hover:bg-slate-100 transition-default"
          >
            <ArrowLeft size={18} />
          </button>
        )}
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <span
            className={`cursor-pointer hover:text-primary-600 ${view === 'categories' ? 'text-primary-600 font-medium' : ''}`}
            onClick={() => { setView('categories'); setSelectedCategory(null); setSelectedProject(null); setProjects([]); }}
          >
            Категории
          </span>
          {selectedCategory && (
            <>
              <ChevronRight size={14} />
              <span
                className={`cursor-pointer hover:text-primary-600 ${view === 'projects' ? 'text-primary-600 font-medium' : ''}`}
                onClick={() => { setView('projects'); setSelectedProject(null); }}
              >
                {selectedCategory.name}
              </span>
            </>
          )}
          {selectedProject && (
            <>
              <ChevronRight size={14} />
              <span className="text-primary-600 font-medium">{selectedProject.name}</span>
            </>
          )}
        </div>
      </div>

      {view === 'categories' && (
        <>
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-slate-900">Категории ({categories.length})</h2>
            <button
              onClick={() => { setShowCategoryForm(true); setEditingCategory(null); setCategoryForm({ name: '', description: '', sort_order: 0, is_active: true }); }}
              className="flex items-center gap-2 px-4 py-2 bg-primary-600 text-white rounded-xl text-sm font-medium hover:bg-primary-700 transition-default"
            >
              <Plus size={16} />
              Добавить
            </button>
          </div>

          {showCategoryForm && (
            <div className="bg-white rounded-2xl border border-slate-200 p-6">
              <h3 className="font-semibold text-slate-900 mb-4">
                {editingCategory ? 'Редактировать категорию' : 'Новая категория'}
              </h3>
              <div className="space-y-4">
                <input
                  placeholder="Название (напр. Рестораны)"
                  value={categoryForm.name}
                  onChange={(e) => setCategoryForm({ ...categoryForm, name: e.target.value })}
                  className="w-full px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default"
                />
                <textarea
                  placeholder="Описание (необязательно)"
                  value={categoryForm.description}
                  onChange={(e) => setCategoryForm({ ...categoryForm, description: e.target.value })}
                  rows={2}
                  className="w-full px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default resize-none"
                />
                <div className="flex items-center gap-4">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="number"
                      value={categoryForm.sort_order}
                      onChange={(e) => setCategoryForm({ ...categoryForm, sort_order: parseInt(e.target.value) || 0 })}
                      className="w-20 px-3 py-2 rounded-lg border border-slate-200 outline-none focus:border-primary-500"
                    />
                    Порядок
                  </label>
                  <label className="flex items-center gap-2 text-sm cursor-pointer">
                    <input
                      type="checkbox"
                      checked={categoryForm.is_active}
                      onChange={(e) => setCategoryForm({ ...categoryForm, is_active: e.target.checked })}
                      className="rounded"
                    />
                    Активна
                  </label>
                </div>
                <div className="flex gap-2">
                  <button onClick={saveCategory} className="px-6 py-2.5 bg-primary-600 text-white rounded-xl text-sm font-medium hover:bg-primary-700 transition-default">
                    {editingCategory ? 'Сохранить' : 'Создать'}
                  </button>
                  <button onClick={() => { setShowCategoryForm(false); setEditingCategory(null); }} className="px-6 py-2.5 bg-slate-100 text-slate-600 rounded-xl text-sm font-medium hover:bg-slate-200 transition-default">
                    Отмена
                  </button>
                </div>
              </div>
            </div>
          )}

          <div className="space-y-3">
            {categories.map((cat) => (
              <div key={cat.id} className="bg-white rounded-2xl border border-slate-200 p-5 flex items-center justify-between hover:shadow-sm transition-default">
                <div className="flex items-center gap-4 cursor-pointer flex-1" onClick={() => openProjects(cat)}>
                  <div className="w-12 h-12 rounded-xl bg-primary-50 text-primary-600 flex items-center justify-center">
                    <FolderOpen size={22} />
                  </div>
                  <div>
                    <div className="font-medium text-slate-900">{cat.name}</div>
                    <div className="text-sm text-slate-500">{cat.projects_count} проектов</div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => copyCategoryLink(cat)} className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition-default" title="Скопировать ссылку">
                    <Link size={16} />
                  </button>
                  <button
                    onClick={() => { setEditingCategory(cat); setCategoryForm({ name: cat.name, description: cat.description, sort_order: cat.sort_order, is_active: cat.is_active }); setShowCategoryForm(true); }}
                    className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition-default"
                  >
                    <Edit size={16} />
                  </button>
                  <button onClick={() => deleteCategory(cat.id)} className="p-2 rounded-lg hover:bg-red-50 text-slate-400 hover:text-red-600 transition-default">
                    <Trash2 size={16} />
                  </button>
                </div>
              </div>
            ))}
            {!loading && categories.length === 0 && (
              <div className="text-center py-16 text-slate-400">
                <FolderOpen size={48} className="mx-auto mb-4 opacity-30" />
                <p className="text-lg font-medium">Нет категорий</p>
                <p className="text-sm mt-1">Создайте первую категорию портфолио</p>
              </div>
            )}
          </div>
        </>
      )}

      {view === 'projects' && selectedCategory && (
        <>
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-slate-900">
              Проекты: {selectedCategory.name} ({projects.length})
            </h2>
            <button
              onClick={() => { setShowProjectForm(true); setEditingProject(null); setProjectForm({ name: '', description: '', client_name: '' }); }}
              className="flex items-center gap-2 px-4 py-2 bg-primary-600 text-white rounded-xl text-sm font-medium hover:bg-primary-700 transition-default"
            >
              <Plus size={16} />
              Добавить
            </button>
          </div>

          {showProjectForm && (
            <div className="bg-white rounded-2xl border border-slate-200 p-6">
              <h3 className="font-semibold text-slate-900 mb-4">
                {editingProject ? 'Редактировать проект' : 'Новый проект'}
              </h3>
              <div className="space-y-4">
                <input
                  placeholder="Название проекта"
                  value={projectForm.name}
                  onChange={(e) => setProjectForm({ ...projectForm, name: e.target.value })}
                  className="w-full px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default"
                />
                <input
                  placeholder="Имя клиента (необязательно)"
                  value={projectForm.client_name}
                  onChange={(e) => setProjectForm({ ...projectForm, client_name: e.target.value })}
                  className="w-full px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default"
                />
                <textarea
                  placeholder="Описание проекта"
                  value={projectForm.description}
                  onChange={(e) => setProjectForm({ ...projectForm, description: e.target.value })}
                  rows={3}
                  className="w-full px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default resize-none"
                />
                <div className="flex gap-2">
                  <button onClick={saveProject} className="px-6 py-2.5 bg-primary-600 text-white rounded-xl text-sm font-medium hover:bg-primary-700 transition-default">
                    {editingProject ? 'Сохранить' : 'Создать'}
                  </button>
                  <button onClick={() => { setShowProjectForm(false); setEditingProject(null); }} className="px-6 py-2.5 bg-slate-100 text-slate-600 rounded-xl text-sm font-medium hover:bg-slate-200 transition-default">
                    Отмена
                  </button>
                </div>
              </div>
            </div>
          )}

          <div className="space-y-3">
            {projects.map((proj) => (
              <div key={proj.id} className="bg-white rounded-2xl border border-slate-200 p-5 flex items-center justify-between hover:shadow-sm transition-default">
                <div className="flex items-center gap-4 cursor-pointer flex-1" onClick={() => openScreenshots(proj)}>
                  <div className="w-12 h-12 rounded-xl bg-blue-50 text-blue-600 flex items-center justify-center">
                    <Image size={22} />
                  </div>
                  <div>
                    <div className="font-medium text-slate-900">{proj.name}</div>
                    <div className="text-sm text-slate-500">
                      {proj.client_name && <span className="mr-2">{proj.client_name}</span>}
                      {proj.screenshots.length} скриншотов
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => copyProjectLink(proj)} className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition-default" title="Скопировать ссылку">
                    <Link size={16} />
                  </button>
                  <button
                    onClick={() => { setEditingProject(proj); setProjectForm({ name: proj.name, description: proj.description, client_name: proj.client_name }); setShowProjectForm(true); }}
                    className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition-default"
                  >
                    <Edit size={16} />
                  </button>
                  <button onClick={() => deleteProject(proj.id)} className="p-2 rounded-lg hover:bg-red-50 text-slate-400 hover:text-red-600 transition-default">
                    <Trash2 size={16} />
                  </button>
                </div>
              </div>
            ))}
            {!loading && projects.length === 0 && (
              <div className="text-center py-16 text-slate-400">
                <Image size={48} className="mx-auto mb-4 opacity-30" />
                <p className="text-lg font-medium">Нет проектов</p>
                <p className="text-sm mt-1">Добавьте первый проект в эту категорию</p>
              </div>
            )}
          </div>
        </>
      )}

      {view === 'screenshots' && selectedProject && (
        <>
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-slate-900">
              Скриншоты: {selectedProject.name} ({selectedProject.screenshots.length})
            </h2>
            <label className={`flex items-center gap-2 px-4 py-2 bg-primary-600 text-white rounded-xl text-sm font-medium hover:bg-primary-700 transition-default cursor-pointer ${uploading ? 'opacity-50 pointer-events-none' : ''}`}>
              <Upload size={16} />
              {uploading ? 'Загрузка...' : 'Загрузить'}
              <input
                type="file"
                multiple
                accept="image/*"
                className="hidden"
                onChange={(e) => e.target.files && uploadScreenshots(e.target.files)}
              />
            </label>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
            {selectedProject.screenshots.map((shot) => (
              <div key={shot.id} className="bg-white rounded-2xl border border-slate-200 overflow-hidden group">
                <div className="aspect-video bg-slate-100 relative cursor-pointer" onClick={() => setPreviewImage(shot.url)}>
                  <img
                    src={shot.url}
                    alt={shot.original_filename}
                    className="w-full h-full object-cover"
                  />
                  <div className="absolute inset-0 bg-black/0 group-hover:bg-black/30 transition-default flex items-center justify-center opacity-0 group-hover:opacity-100">
                    <Eye size={24} className="text-white" />
                  </div>
                </div>
                <div className="p-3 flex items-center justify-between">
                  <span className="text-xs text-slate-500 truncate flex-1">{shot.original_filename}</span>
                  <button onClick={() => deleteScreenshot(shot.id)} className="p-1 rounded hover:bg-red-50 text-slate-400 hover:text-red-600 transition-default">
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            ))}
            {!loading && selectedProject.screenshots.length === 0 && (
              <div className="col-span-full text-center py-16 text-slate-400">
                <Upload size={48} className="mx-auto mb-4 opacity-30" />
                <p className="text-lg font-medium">Нет скриншотов</p>
                <p className="text-sm mt-1">Загрузите скриншоты проекта</p>
              </div>
            )}
          </div>
        </>
      )}

      {previewImage && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 p-4" onClick={() => setPreviewImage(null)}>
          <button className="absolute top-4 right-4 p-2 rounded-full bg-white/10 text-white hover:bg-white/20 transition-default" onClick={() => setPreviewImage(null)}>
            <X size={24} />
          </button>
          <img src={previewImage} alt="" className="max-w-full max-h-full rounded-lg" onClick={(e) => e.stopPropagation()} />
        </div>
      )}
    </div>
  );
}
