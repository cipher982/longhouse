// AST-based migration tool using syn crate
// This would be more precise than regex but requires more setup

use std::fs;
use syn::{parse_file, visit_mut::VisitMut, Type, TypePath, Expr, Member};

pub struct SchemaMigrationVisitor;

impl VisitMut for SchemaMigrationVisitor {
    fn visit_type_mut(&mut self, ty: &mut Type) {
        if let Type::Path(TypePath { path, .. }) = ty {
            if let Some(ident) = path.get_ident() {
                match ident.to_string().as_str() {
                    "Node" => *ident = syn::Ident::new("WorkflowNode", ident.span()),
                    "CanvasNode" => *ident = syn::Ident::new("WorkflowNode", ident.span()),
                    "Edge" => *ident = syn::Ident::new("WorkflowEdge", ident.span()),
                    _ => {}
                }
            }
        }
        syn::visit_mut::visit_type_mut(self, ty);
    }

    fn visit_expr_mut(&mut self, expr: &mut Expr) {
        // Transform field access patterns
        if let Expr::Field(field_expr) = expr {
            if let Member::Named(ident) = &field_expr.member {
                match ident.to_string().as_str() {
                    "nodes" => field_expr.member = Member::Named(syn::Ident::new("workflow_nodes", ident.span())),
                    _ => {}
                }
            }
        }
        syn::visit_mut::visit_expr_mut(self, expr);
    }
}

pub fn migrate_file(file_path: &str) -> Result<(), Box<dyn std::error::Error>> {
    let content = fs::read_to_string(file_path)?;
    let mut ast = parse_file(&content)?;

    let mut visitor = SchemaMigrationVisitor;
    visitor.visit_file_mut(&mut ast);

    let new_content = quote::quote!(#ast).to_string();
    fs::write(file_path, new_content)?;

    Ok(())
}