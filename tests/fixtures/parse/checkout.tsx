/**
 * @file Checkout page and helpers.
 */
import React, { useState } from "react";
import type { Cart } from "../types";
import {
  formatPrice,
  applyTax,
} from "./money";
// import { Nope } from "./commented";
const lazy = import("./lazy");

/** Props for the checkout. */
export interface CheckoutProps extends BaseProps, Themed {
  cart: Cart;
  onPay(amount: number): Promise<void>;
}

export type Status = "idle" | "paying";

/** Renders the checkout. Don't forget tax. */
export const Checkout: React.FC<CheckoutProps> = ({ cart, onPay }) => {
  const handle = () => onPay(1);
  return <p>Don't pay {cart.total}</p>;
};

export default function CheckoutPage() {
  return <Checkout cart={c} onPay={p} />;
}

export class PaymentService extends Base implements Payer {
  private secret = "x";
  /** Charge a card. */
  async charge(card: Card, amount: number): Promise<Receipt> {
    return this.gateway.post(`/charge/${card.id}`, { amount });
  }
  #internal() {}
  static create(): PaymentService { return new PaymentService(); }
  handle = async (e: Event) => { console.log(e); };
}

function helper() {}
const MAX_RETRIES = 3;
export { helper as publicHelper };
