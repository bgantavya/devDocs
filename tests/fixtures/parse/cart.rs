//! Cart engine.
//! Handles pricing.
use std::collections::{HashMap, HashSet};
use crate::money::Price;
use serde::Serialize as Ser;

/// A shopping cart.
#[derive(Debug, Clone)]
pub struct Cart<'a> {
    items: Vec<&'a str>,
}

/// Something priced.
pub trait Priced {
    fn price(&self) -> Price;
}

impl<'a> Cart<'a> {
    /// Build an empty cart.
    pub fn new() -> Self { Cart { items: vec![] } }
    fn internal(&self, s: &'a str) -> &'a str { let c = '}'; s }
}

impl<'a> Priced for Cart<'a>
where
    Self: Sized,
{
    fn price(&self) -> Price { todo!() }
}

pub const MAX: usize = 10;
pub(crate) async fn checkout<'a>(c: &Cart<'a>) -> Result<(), Error> { Ok(()) }
fn private_helper() { let s = r#"fn fake() {"#; }
macro_rules! cart { () => {} }
